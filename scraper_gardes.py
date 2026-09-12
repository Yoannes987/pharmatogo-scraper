"""
PharmaTogo -- Scraper hebdomadaire des pharmacies de garde
Source : https://www.inam.tg/pharmacies-de-garde/

Ce script :
  1. Lit le tableau des pharmacies de garde sur inam.tg
  2. S'arrete tout de suite si la semaine affichee est DEJA en base
     (voir "Pourquoi un arret precoce" plus bas)
  3. Fait correspondre chaque nom a une pharmacie deja presente dans
     la table `pharmacies` (~277 pharmacies importees depuis l'ONTP),
     avec plusieurs strategies de comparaison pour minimiser les ratés
  4. Met a jour est_de_garde + gardes_historique pour les correspondances
  5. Loggue dans pharmacies_non_reconnues uniquement les VRAIS cas
     ou aucune pharmacie proche n'existe deja (probable nouvelle pharmacie
     a ajouter manuellement, ou faute de frappe trop importante sur inam.tg)

Pourquoi un arret precoce
-------------------------
La liste ne change qu'une fois par semaine, mais le workflow passe toutes
les heures les dimanche/lundi/mardi (on ne sait pas quand INAM publie).
Sans garde-fou, c'est ~72 passages qui refont le meme travail. On compare
donc la periode lue sur la page a ce qui est deja dans gardes_historique :
si c'est la meme semaine, on sort immediatement sans rien ecrire.

Pourquoi on ne remet pas les gardes a zero tout de suite
--------------------------------------------------------
L'ancienne version faisait `est_de_garde = false` PARTOUT avant de
commencer les correspondances. Si le script s'interrompait ensuite (coupure
reseau, INAM qui repond a moitie), la base restait avec une liste de garde
vide ou tronquee -- c'est-a-dire une app qui affiche "aucune pharmacie de
garde" un dimanche soir, le pire cas possible. Desormais on calcule TOUTES
les correspondances d'abord, on verifie qu'elles sont plausibles, et on
n'ecrit qu'a la fin.

Variables d'environnement requises (fournies par GitHub Actions secrets) :
  SUPABASE_URL          ex: https://xxxxx.supabase.co
  SUPABASE_SERVICE_KEY  la cle "service_role" / "secret" (jamais la cle publique)

Options :
  --force     ignore l'arret precoce et retraite la semaine meme si elle
              est deja en base (utile apres avoir ajoute des pharmacies
              manquantes dans `pharmacies`)
  --dry-run   analyse et affiche le rapport, sans RIEN ecrire en base
"""

import difflib
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

import cloudscraper
import requests
from bs4 import BeautifulSoup

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
}

INAM_URL = "https://www.inam.tg/pharmacies-de-garde/"

FORCE = "--force" in sys.argv
DRY_RUN = "--dry-run" in sys.argv

# Si la nouvelle liste represente moins que cette fraction de la semaine
# precedente, on considere que quelque chose a mal tourne (page tronquee,
# format modifie) et on n'ecrit rien. Ex: 53 pharmacies la semaine derniere
# et 12 cette semaine -> suspect, on s'arrete.
FRACTION_MINIMALE = 0.5

MOIS_FR = {
    "janvier": 1, "janv": 1,
    "février": 2, "fevrier": 2, "févr": 2, "fevr": 2,
    "mars": 3,
    "avril": 4, "avr": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7, "juil": 7,
    "août": 8, "aout": 8,
    "septembre": 9, "sept": 9,
    "octobre": 10, "oct": 10,
    "novembre": 11, "nov": 11,
    "décembre": 12, "decembre": 12, "déc": 12, "dec": 12,
}

# Mots qu'on ignore quand on compare deux noms par mots-cles
# (trop frequents pour etre discriminants)
MOTS_VIDES = {"PHARMACIE", "DE", "DU", "DES", "LA", "LE", "LES", "D", "L"}

# Abreviations courantes vues sur inam.tg vs l'ONTP, normalisees a l'avance
EQUIVALENCES = {
    "ST": "SAINT",
    "STE": "SAINTE",
}


# ------------------------------------------------------------------
# Normalisation des noms
# ------------------------------------------------------------------

def normaliser(nom):
    """Enleve les accents + tout ce qui n'est pas alphanumerique, en majuscules.
    Identique a la colonne generee `nom_normalise` en base."""
    if not nom:
        return ""
    sans_accents = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]", "", sans_accents).upper()


def normaliser_sans_prefixe(nom):
    """Comme normaliser(), mais retire le mot 'PHARMACIE' qui est present
    dans presque tous les noms et fausse sinon le calcul de ressemblance
    (deux pharmacies totalement differentes partageraient deja 9 lettres
    communes juste a cause de ce mot)."""
    cle = normaliser(nom)
    return cle.replace("PHARMACIE", "", 1) if cle.startswith("PHARMACIE") else cle


def mots_significatifs(nom):
    """Decoupe le nom en mots, enleve les mots vides et applique les
    equivalences (ST -> SAINT), pour une comparaison par mots-cles."""
    if not nom:
        return set()
    sans_accents = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode("ascii")
    mots = re.findall(r"[A-Z0-9]+", sans_accents.upper())
    mots = [EQUIVALENCES.get(m, m) for m in mots]
    return {m for m in mots if m not in MOTS_VIDES and len(m) > 1}


# ------------------------------------------------------------------
# Correspondance en plusieurs passes
# ------------------------------------------------------------------

def trouver_correspondance(nom_brut, pharmacies_db, index_sans_prefixe):
    """Essaie plusieurs strategies dans l'ordre, de la plus stricte a la
    plus permissive. Retourne (pharmacie, methode) ou (None, None).

    pharmacies_db      : liste de dicts {id, nom, nom_normalise}
    index_sans_prefixe : {nom_normalise_sans_prefixe: pharmacie}, calcule
                         une seule fois par le programme principal (le
                         reconstruire a chaque appel etait inutilement lourd)
    """
    cle = normaliser(nom_brut)
    mots_cherches = mots_significatifs(nom_brut)

    # Passe 1 : correspondance exacte sur le nom normalise
    for p in pharmacies_db:
        if p.get("nom_normalise") == cle:
            return p, "exacte"

    # Passe 2 : correspondance floue (typos, ordre des mots legerement different)
    # On compare sans le mot "PHARMACIE" pour ne pas fausser le score de
    # ressemblance entre deux pharmacies par ailleurs sans rapport.
    cle_sans_prefixe = normaliser_sans_prefixe(nom_brut)
    candidats = difflib.get_close_matches(
        cle_sans_prefixe, index_sans_prefixe.keys(), n=1, cutoff=0.60
    )
    if candidats:
        return index_sans_prefixe[candidats[0]], "floue"

    # Passe 3 : correspondance par mots-cles communs (ex: "ST PIERRE" vs
    # "SAINT PIERRE ANNEXE" -> au moins tous les mots significatifs de
    # inam.tg se retrouvent dans le nom de la base, ou l'inverse)
    if mots_cherches:
        meilleur, meilleur_score = None, 0.0
        for p in pharmacies_db:
            mots_db = mots_significatifs(p["nom"])
            if not mots_db:
                continue
            communs = mots_cherches & mots_db
            if not communs:
                continue
            score = len(communs) / max(len(mots_cherches), len(mots_db))
            # bonus si l'un des deux ensembles est entierement inclus dans l'autre
            if mots_cherches <= mots_db or mots_db <= mots_cherches:
                score += 0.3
            if score > meilleur_score:
                meilleur, meilleur_score = p, score
        if meilleur and meilleur_score >= 0.55:
            return meilleur, "mots-cles"

    return None, None


# ------------------------------------------------------------------
# Recuperation de la page INAM
# ------------------------------------------------------------------

def recuperer_page_inam():
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Referer": "https://www.google.com/",
    }
    resp = scraper.get(INAM_URL, timeout=30, headers=headers)
    resp.raise_for_status()
    # INAM ne declare pas de charset alors que ses pages sont en UTF-8 :
    # sans cette ligne les accents arrivent casses ("DÃ©kon").
    resp.encoding = "utf-8"
    return BeautifulSoup(resp.text, "html.parser")


def extraire_periode(soup):
    """Lit la periode de garde affichee, ex. "Liste des pharmacies du 24 au
    31 août 2026" -> (date(2026,8,24), date(2026,8,31)).

    Gere les trois formulations rencontrees :
      - meme mois        "du 24 au 31 août 2026"
      - deux mois        "du 31 août au 6 septembre 2026"
      - deux annees      "du 28 décembre 2026 au 3 janvier 2027"

    Le "au" est OBLIGATOIRE dans le motif. C'est ce qui evite de confondre
    la periode avec les adresses de la page ("Boulevard du 13 Janvier",
    "bd du 30 Août") : l'ancienne version rendait "au" optionnel et pouvait
    attraper n'importe quel "du <nombre> <mot> <annee>".
    """
    texte = soup.get_text(" ", strip=True)
    motif = re.compile(
        r"du\s+(\d{1,2})"                 # jour de debut
        r"(?:\s+([A-Za-zéèûôàç]+))?"      # mois de debut (absent si meme mois)
        r"(?:\s+(\d{4}))?"                # annee de debut (absente si meme annee)
        r"\s+au\s+(\d{1,2})"              # jour de fin
        r"\s+([A-Za-zéèûôàç]+)"           # mois de fin
        r"\s+(\d{4})",                    # annee de fin
        re.IGNORECASE,
    )
    for m in motif.finditer(texte):
        jour_debut, mois_debut_nom, annee_debut, jour_fin, mois_fin_nom, annee_fin = m.groups()

        mois_fin = MOIS_FR.get(mois_fin_nom.lower())
        if not mois_fin:
            continue  # "du 5 au 12 machin 2026" -> pas une periode, on continue
        mois_debut = MOIS_FR.get((mois_debut_nom or "").lower()) or mois_fin

        annee_fin = int(annee_fin)
        if annee_debut:
            annee_debut = int(annee_debut)
        elif mois_debut > mois_fin:
            # "du 28 décembre au 3 janvier 2027" -> le debut est l'annee d'avant
            annee_debut = annee_fin - 1
        else:
            annee_debut = annee_fin

        try:
            debut = datetime(annee_debut, mois_debut, int(jour_debut)).date()
            fin = datetime(annee_fin, mois_fin, int(jour_fin)).date()
        except ValueError:
            continue  # date impossible (31 février...) -> ce n'etait pas la periode
        if fin < debut:
            continue
        return debut, fin
    return None, None


def extraire_pharmacies_de_garde(soup):
    table = soup.find("table")
    if table is None:
        raise RuntimeError("Aucun tableau trouve sur la page INAM -- la structure a peut-etre change.")
    lignes = []
    for tr in table.find_all("tr")[1:]:  # skip header row
        cellules = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(cellules) < 3:
            continue
        nom_brut, tel_brut, emplacement_brut = cellules[0], cellules[1], cellules[2]
        if not nom_brut:
            continue
        lignes.append({
            "nom_brut": nom_brut.strip(),
            "telephone_brut": tel_brut.replace("☎", "").strip(),
            "emplacement_brut": emplacement_brut.strip(),
        })
    return lignes


# ------------------------------------------------------------------
# Appels Supabase
# ------------------------------------------------------------------

def recuperer_pharmacies_existantes():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/pharmacies",
        headers=HEADERS,
        params={"select": "id,nom,nom_normalise", "limit": "5000"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def derniere_semaine_en_base():
    """(semaine_debut, semaine_fin, nombre_de_pharmacies) de la periode la
    plus recente deja enregistree, ou (None, None, 0) si la table est vide.
    Sert a l'arret precoce et au controle de vraisemblance."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/gardes_historique",
        headers=HEADERS,
        params={
            "select": "semaine_debut,semaine_fin",
            "order": "semaine_debut.desc",
            "limit": "1",
        },
        timeout=30,
    )
    r.raise_for_status()
    lignes = r.json()
    if not lignes:
        return None, None, 0
    debut, fin = lignes[0]["semaine_debut"], lignes[0]["semaine_fin"]
    # Compte les pharmacies de cette semaine-la (Prefer: count=exact renvoie
    # le total dans l'en-tete Content-Range, sans telecharger les lignes).
    r2 = requests.get(
        f"{SUPABASE_URL}/rest/v1/gardes_historique",
        headers={**HEADERS, "Prefer": "count=exact"},
        params={"select": "id", "semaine_debut": f"eq.{debut}", "limit": "1"},
        timeout=30,
    )
    r2.raise_for_status()
    total = 0
    plage = r2.headers.get("Content-Range", "")
    if "/" in plage:
        try:
            total = int(plage.split("/")[1])
        except ValueError:
            total = 0
    return debut, fin, total


def appliquer_gardes(ids_de_garde, semaine_debut, semaine_fin):
    """Ecrit la nouvelle liste de garde en 3 requetes groupees au lieu de
    deux par pharmacie (l'ancienne version faisait ~106 appels HTTP pour
    53 pharmacies, autant d'occasions d'echouer a mi-parcours)."""
    maintenant = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    # 1. Tout le monde a false
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/pharmacies",
        headers=HEADERS,
        params={"est_de_garde": "is.true"},
        json={"est_de_garde": False},
        timeout=60,
    )
    r.raise_for_status()

    if not ids_de_garde:
        return

    # 2. Les pharmacies de garde a true, en un seul appel
    liste = ",".join(str(i) for i in ids_de_garde)
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/pharmacies",
        headers=HEADERS,
        params={"id": f"in.({liste})"},
        json={"est_de_garde": True, "derniere_maj_garde": maintenant},
        timeout=60,
    )
    r.raise_for_status()

    # 3. Historique, en un seul upsert groupe
    if semaine_debut and semaine_fin:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/gardes_historique",
            headers={**HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
            # sans on_conflict, merge-duplicates ne sait pas quelle
            # contrainte utiliser et Postgres refuse (409)
            params={"on_conflict": "pharmacie_id,semaine_debut"},
            json=[
                {
                    "pharmacie_id": pid,
                    "semaine_debut": semaine_debut.isoformat(),
                    "semaine_fin": semaine_fin.isoformat(),
                    "source": "inam.tg",
                }
                for pid in ids_de_garde
            ],
            timeout=60,
        )
        r.raise_for_status()


def logguer_non_reconnues(entrees, semaine_debut):
    if not entrees:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/pharmacies_non_reconnues",
        headers={**HEADERS, "Prefer": "return=minimal"},
        json=[
            {
                "nom_brut": e["nom_brut"],
                "telephone_brut": e["telephone_brut"],
                "emplacement_brut": e["emplacement_brut"],
                "semaine_debut": semaine_debut.isoformat() if semaine_debut else None,
            }
            for e in entrees
        ],
        timeout=30,
    )
    r.raise_for_status()


# ------------------------------------------------------------------
# Programme principal
# ------------------------------------------------------------------

def main():
    print("Recuperation de la page INAM...")
    soup = recuperer_page_inam()

    semaine_debut, semaine_fin = extraire_periode(soup)
    print(f"Periode detectee : {semaine_debut} -> {semaine_fin}")

    # La periode est indispensable : sans elle, on marquerait les nouvelles
    # gardes sans mettre a jour l'historique, et l'app afficherait la liste
    # de cette semaine sous le titre de la semaine derniere.
    if not semaine_debut or not semaine_fin:
        print("ERREUR : periode 'du ... au ...' introuvable sur la page.")
        print("-> Formulation probablement modifiee. Arret, aucune donnee touchee.")
        sys.exit(1)

    dernier_debut, dernier_fin, dernier_total = derniere_semaine_en_base()
    print(f"Derniere semaine en base : {dernier_debut} -> {dernier_fin} ({dernier_total} pharmacies)")

    # Arret precoce : rien de nouveau a faire.
    if dernier_debut == semaine_debut.isoformat() and not FORCE:
        print("Cette semaine est deja enregistree -> rien a faire.")
        print("   (utiliser --force pour la retraiter malgre tout)")
        return

    entrees = extraire_pharmacies_de_garde(soup)
    print(f"{len(entrees)} pharmacies de garde trouvees sur inam.tg")

    if len(entrees) == 0:
        print("ERREUR : aucune pharmacie trouvee sur la page INAM.")
        print("-> Le format de la page a peut-etre change. Arret, aucune donnee modifiee.")
        sys.exit(1)

    # Controle de vraisemblance : une chute brutale du nombre de pharmacies
    # est presque toujours le signe d'une page tronquee, pas d'une vraie
    # semaine calme.
    if dernier_total and len(entrees) < dernier_total * FRACTION_MINIMALE:
        print(f"ERREUR : {len(entrees)} pharmacies contre {dernier_total} la semaine derniere.")
        print("-> Chute suspecte. Arret par securite, aucune donnee modifiee.")
        sys.exit(1)

    pharmacies_db = recuperer_pharmacies_existantes()
    print(f"{len(pharmacies_db)} pharmacies dans la base (reference ONTP)")

    # Index construit une seule fois (et non a chaque nom cherche).
    index_sans_prefixe = {
        normaliser_sans_prefixe(p["nom"]): p
        for p in pharmacies_db if p.get("nom_normalise")
    }

    # --- Phase 1 : on calcule TOUT, sans rien ecrire ---
    ids_de_garde, non_reconnues = [], []
    for entree in entrees:
        pharmacie, methode = trouver_correspondance(
            entree["nom_brut"], pharmacies_db, index_sans_prefixe
        )
        if pharmacie:
            if pharmacie["id"] not in ids_de_garde:
                ids_de_garde.append(pharmacie["id"])
            if methode != "exacte":
                print(f"  [{methode}] '{entree['nom_brut']}' -> '{pharmacie['nom']}'")
        else:
            non_reconnues.append(entree)
            print(f"  [NON RECONNUE] '{entree['nom_brut']}' -- possible nouvelle pharmacie a ajouter")

    print()
    print(f"{len(ids_de_garde)} associees, {len(non_reconnues)} non reconnues")

    if not ids_de_garde:
        print("ERREUR : aucune correspondance trouvee.")
        print("-> Les noms sur inam.tg ne correspondent plus du tout a la base.")
        print("   Arret par securite : l'ancienne liste de garde est conservee.")
        sys.exit(1)

    if DRY_RUN:
        print("\n--- MODE TEST : rien n'est ecrit en base ---")
        return

    # --- Phase 2 : ecriture, seulement maintenant que tout est verifie ---
    print("Application en base...")
    appliquer_gardes(ids_de_garde, semaine_debut, semaine_fin)
    logguer_non_reconnues(non_reconnues, semaine_debut)

    print(f"\nTermine : semaine du {semaine_debut} au {semaine_fin}, "
          f"{len(ids_de_garde)} pharmacies de garde.")
    if non_reconnues:
        print("-> Va voir la table pharmacies_non_reconnues dans Supabase.")
        print("   Chaque ligne est probablement une pharmacie absente de la")
        print("   liste ONTP initiale -- a ajouter manuellement dans `pharmacies`.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        sys.exit(1)
