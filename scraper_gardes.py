"""
PharmaTogo -- Scraper hebdomadaire des pharmacies de garde
Source : https://www.inam.tg/pharmacies-de-garde/

Ce script :
  1. Lit le tableau des pharmacies de garde sur inam.tg
  2. Fait correspondre chaque nom a une pharmacie deja presente dans
     la table `pharmacies` (277 pharmacies importees depuis l'ONTP),
     avec plusieurs strategies de comparaison pour minimiser les ratés
  3. Met a jour est_de_garde + gardes_historique pour les correspondances
  4. Loggue dans pharmacies_non_reconnues uniquement les VRAIS cas
     ou aucune pharmacie proche n'existe deja (probable nouvelle pharmacie
     a ajouter manuellement, ou faute de frappe trop importante sur inam.tg)

Variables d'environnement requises (fournies par GitHub Actions secrets) :
  SUPABASE_URL          ex: https://xxxxx.supabase.co
  SUPABASE_SERVICE_KEY  la cle "service_role" / "secret" (jamais la cle publique)
"""

import difflib
import os
import re
import sys
import unicodedata
from datetime import datetime

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

MOIS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
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

def trouver_correspondance(nom_brut, pharmacies_db):
    """Essaie plusieurs strategies dans l'ordre, de la plus stricte a la
    plus permissive. Retourne (pharmacie, methode) ou (None, None).

    pharmacies_db : liste de dicts {id, nom, nom_normalise}
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
    index = {
        normaliser_sans_prefixe(p["nom"]): p
        for p in pharmacies_db if p.get("nom_normalise")
    }
    candidats = difflib.get_close_matches(cle_sans_prefixe, index.keys(), n=1, cutoff=0.60)
    if candidats:
        return index[candidats[0]], "floue"

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
    return BeautifulSoup(resp.text, "html.parser")


def extraire_periode(soup):
    """Cherche un texte du type 'du 20 au 27 juillet 2026' sur la page."""
    texte = soup.get_text(" ", strip=True)
    m = re.search(
        r"du\s+(\d{1,2})\s*(?:au)?\s*(?:(\d{1,2})\s+)?(\w+)\s+(\d{4})",
        texte, re.IGNORECASE,
    )
    if not m:
        return None, None
    jour_debut = int(m.group(1))
    jour_fin = int(m.group(2)) if m.group(2) else jour_debut
    mois_nom = m.group(3).lower()
    annee = int(m.group(4))
    mois = MOIS_FR.get(mois_nom)
    if not mois:
        return None, None
    try:
        debut = datetime(annee, mois, jour_debut).date()
        fin = datetime(annee, mois, jour_fin).date()
    except ValueError:
        return None, None
    return debut, fin


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
        params={"select": "id,nom,nom_normalise"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def reinitialiser_gardes():
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/pharmacies",
        headers=HEADERS,
        params={"id": "gt.0"},
        json={"est_de_garde": False},
        timeout=30,
    )
    r.raise_for_status()


def marquer_de_garde(pharmacie_id):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/pharmacies",
        headers=HEADERS,
        params={"id": f"eq.{pharmacie_id}"},
        json={
            "est_de_garde": True,
            "derniere_maj_garde": datetime.utcnow().isoformat(),
        },
        timeout=30,
    )
    r.raise_for_status()


def enregistrer_historique(pharmacie_id, semaine_debut, semaine_fin):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/gardes_historique",
        headers={**HEADERS, "Prefer": "resolution=merge-duplicates"},
        json={
            "pharmacie_id": pharmacie_id,
            "semaine_debut": semaine_debut.isoformat(),
            "semaine_fin": semaine_fin.isoformat(),
            "source": "inam.tg",
        },
        timeout=30,
    )
    r.raise_for_status()


def logguer_non_reconnue(entree, semaine_debut):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/pharmacies_non_reconnues",
        headers=HEADERS,
        json={
            "nom_brut": entree["nom_brut"],
            "telephone_brut": entree["telephone_brut"],
            "emplacement_brut": entree["emplacement_brut"],
            "semaine_debut": semaine_debut.isoformat() if semaine_debut else None,
        },
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

    entrees = extraire_pharmacies_de_garde(soup)
    print(f"{len(entrees)} pharmacies de garde trouvees sur inam.tg")

    pharmacies_db = recuperer_pharmacies_existantes()
    print(f"{len(pharmacies_db)} pharmacies dans la base (reference ONTP)")

    print("Reinitialisation des gardes (tout le monde a false)...")
    reinitialiser_gardes()

    trouvees, non_trouvees = 0, 0
    for entree in entrees:
        pharmacie, methode = trouver_correspondance(entree["nom_brut"], pharmacies_db)

        if pharmacie:
            marquer_de_garde(pharmacie["id"])
            if semaine_debut and semaine_fin:
                enregistrer_historique(pharmacie["id"], semaine_debut, semaine_fin)
            trouvees += 1
            if methode != "exacte":
                print(f"  [{methode}] '{entree['nom_brut']}' -> '{pharmacie['nom']}'")
        else:
            logguer_non_reconnue(entree, semaine_debut)
            non_trouvees += 1
            print(f"  [NON RECONNUE] '{entree['nom_brut']}' -- possible nouvelle pharmacie a ajouter")

    print()
    print(f"Termine : {trouvees} associees, {non_trouvees} non reconnues.")
    if non_trouvees > 0:
        print("-> Va voir la table pharmacies_non_reconnues dans Supabase.")
        print("   Chaque ligne restante est probablement une pharmacie absente")
        print("   de la liste ONTP initiale -- a ajouter manuellement dans `pharmacies`.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        sys.exit(1)
