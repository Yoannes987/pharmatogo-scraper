"""
PharmaTogo — Scraper hebdomadaire des pharmacies de garde
Source : https://www.inam.tg/pharmacies-de-garde/
Met a jour Supabase : pharmacies.est_de_garde, gardes_historique,
et loggue les noms non reconnus dans pharmacies_non_reconnues.

Variables d'environnement requises (fournies par GitHub Actions secrets) :
  SUPABASE_URL         ex: https://xxxxx.supabase.co
  SUPABASE_SERVICE_KEY la cle "service_role" (jamais la cle publique anon)
"""

import os
import re
import sys
from datetime import datetime

import cloudscraper
from bs4 import BeautifulSoup
import requests

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


def normaliser(nom):
    """Meme logique que la colonne generee nom_normalise en base."""
    return re.sub(r"[^a-zA-Z0-9]", "", nom or "").upper()


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
        raise RuntimeError("Aucun tableau trouve sur la page INAM — la structure a peut-etre change.")
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
    """Remet est_de_garde a false pour tout le monde avant d'appliquer la nouvelle liste."""
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


def main():
    print("Recuperation de la page INAM...")
    soup = recuperer_page_inam()

    semaine_debut, semaine_fin = extraire_periode(soup)
    print(f"Periode detectee : {semaine_debut} -> {semaine_fin}")

    entrees = extraire_pharmacies_de_garde(soup)
    print(f"{len(entrees)} pharmacies de garde trouvees sur inam.tg")

    pharmacies_db = recuperer_pharmacies_existantes()
    index_normalise = {p["nom_normalise"]: p for p in pharmacies_db if p.get("nom_normalise")}

    print("Reinitialisation des gardes (tout le monde a false)...")
    reinitialiser_gardes()

    trouvees, non_trouvees = 0, 0
    for entree in entrees:
        cle = normaliser(entree["nom_brut"])
        pharmacie = index_normalise.get(cle)

        if pharmacie is None:
            # tentative simple : essaye sans le mot PHARMACIE en double, ou en sous-chaine
            for cle_db, p in index_normalise.items():
                if cle in cle_db or cle_db in cle:
                    pharmacie = p
                    break

        if pharmacie:
            marquer_de_garde(pharmacie["id"])
            if semaine_debut and semaine_fin:
                enregistrer_historique(pharmacie["id"], semaine_debut, semaine_fin)
            trouvees += 1
        else:
            logguer_non_reconnue(entree, semaine_debut)
            non_trouvees += 1

    print(f"Termine : {trouvees} associees, {non_trouvees} non reconnues (a verifier manuellement).")
    if non_trouvees > 0:
        print("-> Va voir la table pharmacies_non_reconnues dans Supabase pour les traiter.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        sys.exit(1)
