"""
PharmaTogo -- Scraper mensuel du catalogue des medicaments
Source : https://www.inam.tg/remboursement-produits-pharmaceutiques/

Pourquoi ce script existe
-------------------------
Le catalogue etait au depart importe a la main depuis un PDF (2886 lignes).
INAM publie en ligne une liste plus complete (4811 medicaments distincts)
avec le detail du remboursement. Ce script la recupere automatiquement, ce
qui evite d'avoir a refaire un import manuel chaque fois qu'un medicament
apparait ou qu'un prix change.

Comment la page est faite (verifie le 2026-08-24)
-------------------------------------------------
La pagination affichee sur le site est purement decorative : INAM envoie
la TOTALITE du tableau (~25 Mo) en une seule reponse HTTP, et les boutons
"Detail" ouvrent des fenetres (modals) DEJA presentes dans ce meme HTML.
Autrement dit : une seule requete suffit pour tout recuperer, il n'y a ni
page suivante a suivre ni bouton a simuler.

  <tr><td class="Nom"><a data-target="#1"><b>NOM</b>
      <span class="badge"><em>Remboursable</em></span></a></td>
      <td class="TypeMedicament">...</td>
      <td class="GroupeTherapeutique">...</td> ...
  <div class="modal fade" id="1"> ...Quantite: / Unite: / Dosage: / Code: /
      Dci: / Emballage: / Forme: / Prix Public: / Base Remboursement: /
      Taux: / Statut: ... </div>

Le lien ligne <-> detail se fait par l'identifiant du modal (data-target).

Encodage : la page ne declare AUCUN charset alors qu'elle est en UTF-8.
Sans forcage explicite, requests devine du latin-1 et la base se remplit
de "COMPRIMÃ‰S". D'ou le `resp.encoding = "utf-8"` plus bas.

Strategie d'ecriture
--------------------
`code` (ex "0000216") est l'identifiant INAM et porte une contrainte UNIQUE
en base. On envoie donc tout en upsert (merge-duplicates) :
  - code deja connu  -> la ligne est mise a jour (prix, taux...)
  - code nouveau     -> la ligne est ajoutee
Aucun doublon possible, et c'est ce qui rend le passage mensuel sans risque.

Variables d'environnement (secrets GitHub Actions) :
  SUPABASE_URL          ex: https://xxxxx.supabase.co
  SUPABASE_SERVICE_KEY  la cle "service_role" (jamais la cle publique)

Options :
  --dry-run   analyse la page et affiche un rapport, sans RIEN ecrire en base
"""

import os
import re
import sys
import html as html_module
from datetime import datetime, timezone

import cloudscraper
import requests
from bs4 import BeautifulSoup

INAM_URL = "https://www.inam.tg/remboursement-produits-pharmaceutiques/"

# En dessous de ce nombre de medicaments, on considere que la page est
# incomplete ou que son format a change : on s'arrete sans rien ecrire.
# Repere (2026-08-24) : la page contient 14 337 lignes de tableau qui se
# reduisent a 4811 codes uniques ; l'ancien import PDF en avait 2886.
SEUIL_MINIMUM = 3500

# Envoi par paquets : 14 000 lignes d'un coup depassent les limites de
# taille de requete de PostgREST.
TAILLE_LOT = 500

DRY_RUN = "--dry-run" in sys.argv


# ------------------------------------------------------------------
# Recuperation de la page
# ------------------------------------------------------------------

def recuperer_page():
    """Telecharge la page complete. ~25 Mo, compter 30-60 s."""
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Referer": "https://www.google.com/",
    }
    resp = scraper.get(INAM_URL, timeout=180, headers=headers)
    resp.raise_for_status()
    # INAM ne declare pas de charset -- sans cette ligne, tous les accents
    # arrivent casses en base.
    resp.encoding = "utf-8"
    return resp.text


# ------------------------------------------------------------------
# Extraction
# ------------------------------------------------------------------

def nettoyer(valeur):
    """Espaces insecables, espaces multiples, chaine vide -> None."""
    if valeur is None:
        return None
    v = html_module.unescape(valeur).replace("\xa0", " ")
    v = re.sub(r"\s+", " ", v).strip()
    return v or None


def en_nombre(valeur):
    """'7530 FCFA' -> 7530.0 ; '1 250,50' -> 1250.5 ; '' -> None.
    Tolere les espaces separateurs de milliers et la virgule decimale."""
    v = nettoyer(valeur)
    if not v:
        return None
    v = v.replace("FCFA", "").replace("%", "")
    v = re.sub(r"[\s ]", "", v).replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", v)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def en_entier(valeur):
    """Comme en_nombre() mais arrondi a l'entier.

    Necessaire parce que les colonnes `prix_public`, `base_remboursement`,
    `taux_prise_en_charge` et `quantite_presentation` sont de type INTEGER
    en base : envoyer 6092.0 declenche une erreur PostgREST
    (22P02 invalid input syntax for type integer). Seules `part_inam` et
    `part_beneficiaire` sont numeric et acceptent des decimales."""
    v = en_nombre(valeur)
    return None if v is None else int(round(v))


def lire_champs_modal(modal):
    """Une fenetre 'Detail' -> {champ: valeur}.

    Chaque info est un paragraphe propre : <p>Dosage: 200MG</p>. On lit
    donc UNIQUEMENT les <p> (et pas leurs parents), sinon le texte d'un
    bloc se retrouve colle a celui du suivant ('200MG Code: 0000216')."""
    champs = {}
    for p in modal.find_all("p"):
        texte = nettoyer(p.get_text(" ", strip=True))
        if not texte or ":" not in texte:
            continue
        cle, _, valeur = texte.partition(":")
        cle = nettoyer(cle)
        valeur = nettoyer(valeur)
        if cle and valeur and len(cle) <= 40:
            champs[cle.lower()] = valeur
    return champs


def choisir_prix_representatif(lignes):
    """INAM liste parfois le meme `code` plusieurs fois avec des PRIX
    DIFFERENTS (constate le 2026-08-24 : jusqu'a 6 prix pour un meme code,
    ex. PARACETAMOL CREAT 500 -> 780 / 805 / 185 FCFA). Sa page se
    contredit elle-meme et il n'existe aucune regle fiable pour deviner
    le "vrai" prix (la derniere ligne ne correspond au prix le plus
    frequent que dans 50 % des cas).

    Choix retenu : on garde la LIGNE COMPLETE dont le prix est le plus
    frequent (departage par la derniere occurrence). Garder la ligne
    entiere -- et non le prix seul -- evite de melanger le prix d'une
    version avec la base de remboursement d'une autre."""
    from collections import Counter

    avec_prix = [l for l in lignes if l["prix_public"] is not None]
    if not avec_prix:
        return lignes[-1]  # aucune n'a de prix : peu importe laquelle
    freq = Counter(l["prix_public"] for l in avec_prix)
    prix_mode = freq.most_common(1)[0][0]
    representatives = [l for l in avec_prix if l["prix_public"] == prix_mode]
    return representatives[-1]


def extraire_medicaments(html_texte):
    soup = BeautifulSoup(html_texte, "html.parser")

    # Fenetres detail, indexees par leur id (le lien de la ligne pointe
    # dessus via data-target="#<id>").
    details = {
        m.get("id"): lire_champs_modal(m)
        for m in soup.select("div.modal")
        if m.get("id")
    }

    # NB : on boucle sur les <td class="Nom"> et PAS sur les <tr>. Le HTML
    # d'INAM est invalide (des <div> a l'interieur des <tr>), ce qui pousse
    # le parseur a fusionner des lignes et en perd les deux tiers si on
    # itere par <tr>.
    lignes = []
    for cellule_nom in soup.find_all("td", class_="Nom"):
        lien = cellule_nom.find("a")
        # Le nom est dans <b> ; le <span class="badge"> voisin porte le
        # statut ("Remboursable") et ne doit pas etre colle au nom.
        balise_nom = (lien or cellule_nom).find("b")
        nom = nettoyer(balise_nom.get_text(" ", strip=True)) if balise_nom else None
        if not nom:
            continue

        badge = cellule_nom.find("span", class_="badge")
        statut_ligne = nettoyer(badge.get_text(" ", strip=True)) if badge else None

        cible = (lien.get("data-target") if lien else None) or ""
        champs = details.get(cible.lstrip("#"), {})

        c = lambda *noms: next(  # noqa: E731 - lecture tolerante des libelles
            (champs[n] for n in noms if n in champs), None
        )

        code = c("code")
        if not code:  # sans code, pas d'upsert possible -> on ignore
            continue

        prix = en_entier(c("prix public", "prix"))
        base = en_entier(c("base remboursement", "base de remboursement"))
        taux = en_entier(c("taux", "taux de prise en charge"))

        # part_inam / part_beneficiaire ne sont pas publies par INAM :
        # ce sont ces colonnes qui permettront a l'app d'afficher
        # "pris en charge a 80 %, tu paies X FCFA".
        part_inam = round(base * taux / 100, 2) if base is not None and taux is not None else None
        part_benef = (
            round(prix - part_inam, 2)
            if prix is not None and part_inam is not None
            else None
        )

        lignes.append({
            "code": code,
            "nom_commercial": nom,
            "dci": c("dci"),
            "dosage": c("dosage"),
            "forme": c("forme"),
            "groupe_therapeutique": c("groupe therapeutique", "groupe thérapeutique"),
            "type_medicament": c("type medicament", "type médicament"),
            "quantite_presentation": en_entier(c("quantite", "quantité")),
            "unite_presentation": c("unite", "unité"),
            "type_emballage": c("emballage"),
            "prix_public": prix,
            "base_remboursement": base,
            "taux_prise_en_charge": taux,
            "part_inam": part_inam,
            "part_beneficiaire": part_benef,
            "statut": c("statut") or statut_ligne,
            # `derniere_maj` est un `timestamp without time zone` : on envoie
            # une date UTC sans decalage pour eviter toute ambiguite.
            "derniere_maj": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        })

    # Regroupe par code et ne garde qu'une ligne representative par code
    # (voir choisir_prix_representatif), en preservant l'ordre d'apparition.
    from collections import OrderedDict

    par_code = OrderedDict()
    for ligne in lignes:
        par_code.setdefault(ligne["code"], []).append(ligne)

    return [choisir_prix_representatif(grp) for grp in par_code.values()]


# ------------------------------------------------------------------
# Ecriture Supabase
# ------------------------------------------------------------------

def envoyer(medicaments, url, headers):
    total = 0
    for debut in range(0, len(medicaments), TAILLE_LOT):
        lot = medicaments[debut:debut + TAILLE_LOT]
        r = requests.post(
            f"{url}/rest/v1/medicaments",
            headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            params={"on_conflict": "code"},
            json=lot,
            timeout=120,
        )
        r.raise_for_status()
        total += len(lot)
        print(f"  ... {total}/{len(medicaments)}")
    return total


# ------------------------------------------------------------------
# Programme principal
# ------------------------------------------------------------------

def main():
    print("Recuperation de la page INAM (~25 Mo, patience)...")
    html_texte = recuperer_page()
    print(f"  {len(html_texte) / 1024 / 1024:.1f} Mo recus")

    print("Analyse...")
    medicaments = extraire_medicaments(html_texte)
    print(f"  {len(medicaments)} medicaments extraits")

    # Garde-fou : mieux vaut ne rien faire qu'ecraser un bon catalogue
    # avec une page tronquee ou au format modifie.
    if len(medicaments) < SEUIL_MINIMUM:
        print(f"ERREUR : seulement {len(medicaments)} medicaments (< {SEUIL_MINIMUM} attendus).")
        print("-> Page incomplete ou format modifie. Arret, aucune donnee touchee.")
        sys.exit(1)

    avec_prix = sum(1 for m in medicaments if m["prix_public"] is not None)
    avec_taux = sum(1 for m in medicaments if m["taux_prise_en_charge"] is not None)
    print(f"  dont {avec_prix} avec prix et {avec_taux} avec taux de prise en charge")

    if DRY_RUN:
        print("\n--- MODE TEST : rien n'est ecrit en base ---")
        for m in medicaments[:3]:
            print()
            for cle, valeur in m.items():
                print(f"    {cle:24s} : {valeur}")
        return

    url = os.environ["SUPABASE_URL"].rstrip("/")
    cle = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {
        "apikey": cle,
        "Authorization": f"Bearer {cle}",
        "Content-Type": "application/json",
    }

    print("Envoi vers Supabase (upsert sur `code`)...")
    total = envoyer(medicaments, url, headers)
    print(f"\nTermine : {total} medicaments crees ou mis a jour.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        sys.exit(1)
