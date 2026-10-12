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
Sans garde-fou, ce sont ~90 passages qui refont le meme travail. On compare
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
from datetime import datetime, timezone, timedelta

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


def _nettoyer_texte(texte):
    """Rend le texte de la page 'propre' avant toute recherche : supprime les
    caracteres invisibles (espace de largeur nulle, trait d'union mou, BOM),
    transforme les espaces insecables en espaces normaux et fusionne les
    espaces multiples. Un seul caractere invisible entre '12' et 'au' suffit
    sinon a faire rater la detection de la periode."""
    texte = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]", "", texte)
    texte = texte.replace("\xa0", " ").replace("\u202f", " ")
    return re.sub(r"\s+", " ", texte)


def _sans_accents_minuscules(mot):
    return unicodedata.normalize("NFKD", mot).encode("ascii", "ignore").decode("ascii").lower().strip(". ")


_MOIS_NORMALISES = {_sans_accents_minuscules(k): v for k, v in MOIS_FR.items()}


def _numero_mois(mot):
    """'octobre' / 'Octobre' / 'oct.' / 'OCT' / 'févr' -> numero du mois,
    ou None si ce n'est pas un mois."""
    m = _sans_accents_minuscules(mot or "")
    if not m:
        return None
    if m in _MOIS_NORMALISES:
        return _MOIS_NORMALISES[m]
    if len(m) >= 3:  # abreviation : on accepte un debut de nom de mois
        candidats = {v for k, v in _MOIS_NORMALISES.items() if len(k) > 3 and k.startswith(m)}
        if len(candidats) == 1:
            return candidats.pop()
    return None


# "du 12 au 19 octobre 2026", "du 1er au 8 nov.", "du 31 août au 6 septembre 2026",
# "du 28 décembre 2026 au 3 janvier 2027", "Du 12 a 19 Octobre" (annee facultative)
_MOTIF_PERIODE = re.compile(
    r"\bdu\s+(\d{1,2})\s*(?:er|e)?"          # jour de debut (12, 1er)
    r"(?:\s+([^\W\d_]+)\.?)?"                  # mois de debut (absent si meme mois)
    r"(?:\s+(\d{4}))?"                          # annee de debut (facultative)
    r"\s+(?:au|a|\u00e0)\s+(\d{1,2})\s*(?:er|e)?"  # separateur + jour de fin
    r"\s+([^\W\d_]+)\.?"                       # mois de fin
    r"(?:\s+(\d{4}))?",                         # annee de fin (facultative)
    re.IGNORECASE,
)

# Variante numerique : "du 12/10/2026 au 19/10/2026"
_MOTIF_PERIODE_NUM = re.compile(
    r"\bdu\s+(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\s+au\s+(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})",
    re.IGNORECASE,
)


def _periode_valide(debut, fin):
    # Une periode de garde dure environ une semaine. Refuser tout ce qui est
    # hors de 1-14 jours evite de prendre une adresse ("Boulevard du 13
    # Janvier au ...") pour la periode.
    return 1 <= (fin - debut).days <= 14


def extraire_periode(soup):
    """Lit la periode de garde affichee, ex. "Liste des pharmacies du 12 au
    19 octobre 2026" -> (date(2026,10,12), date(2026,10,19)).

    Tolerant : accents ou non, majuscules ou minuscules, mois abrege
    ("oct."), "1er", annee absente, caracteres invisibles, espaces
    insecables, balises collees entre les mots. Le "au" reste obligatoire
    (c'est ce qui evite de confondre avec "Bd du 13 Janvier"), et la duree
    doit etre de 1 a 14 jours.

    Si rien n'est trouve, affiche dans le journal le morceau de texte autour
    de "pharmacies du" : la prochaine fois qu'INAM change sa phrase, on la
    voit tout de suite dans les logs au lieu de deviner.
    """
    texte = _nettoyer_texte(soup.get_text(" ", strip=True))
    aujourdhui = datetime.now(timezone.utc).date()

    for m in _MOTIF_PERIODE.finditer(texte):
        jour_debut, mois_debut_nom, annee_debut, jour_fin, mois_fin_nom, annee_fin = m.groups()
        mois_fin = _numero_mois(mois_fin_nom)
        if not mois_fin:
            continue  # "du 5 au 12 machin" -> pas une periode
        mois_debut = _numero_mois(mois_debut_nom) or mois_fin

        annee_fin = int(annee_fin) if annee_fin else None
        annee_debut = int(annee_debut) if annee_debut else None
        if annee_fin is None and annee_debut is None:
            # Aucune annee ecrite : on prend l'annee qui rend la periode la
            # plus proche d'aujourd'hui (gere decembre -> janvier).
            candidats = []
            for a in (aujourdhui.year - 1, aujourdhui.year, aujourdhui.year + 1):
                try:
                    d = datetime(a, mois_debut, int(jour_debut)).date()
                    f = datetime(a + (1 if mois_debut > mois_fin else 0), mois_fin, int(jour_fin)).date()
                except ValueError:
                    continue
                if _periode_valide(d, f):
                    candidats.append((abs((d - aujourdhui).days), d, f))
            if not candidats:
                continue
            _, debut, fin = min(candidats)
        else:
            if annee_fin is None:
                annee_fin = annee_debut + (1 if mois_debut > mois_fin else 0)
            if annee_debut is None:
                # "du 28 decembre au 3 janvier 2027" -> le debut est l'annee d'avant
                annee_debut = annee_fin - 1 if mois_debut > mois_fin else annee_fin
            try:
                debut = datetime(annee_debut, mois_debut, int(jour_debut)).date()
                fin = datetime(annee_fin, mois_fin, int(jour_fin)).date()
            except ValueError:
                continue  # date impossible (31 fevrier...)
        if _periode_valide(debut, fin):
            return debut, fin

    for m in _MOTIF_PERIODE_NUM.finditer(texte):
        j1, m1, a1, j2, m2, a2 = (int(x) for x in m.groups())
        try:
            debut, fin = datetime(a1, m1, j1).date(), datetime(a2, m2, j2).date()
        except ValueError:
            continue
        if _periode_valide(debut, fin):
            return debut, fin

    # Rien trouve : on laisse une trace lisible pour comprendre pourquoi.
    bas = texte.lower()
    for repere in ("pharmacies du", "liste des pharmacies", "pharmacies de garde"):
        i = bas.find(repere)
        if i != -1:
            print(f"  Texte autour de '{repere}' : {texte[max(0, i - 40):i + 160]!r}")
            break
    else:
        print(f"  Debut du texte de la page : {texte[:200]!r}")
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


def ids_en_garde_actuels():
    """Ensemble des id actuellement marques de garde dans `pharmacies`."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/pharmacies",
        headers=HEADERS,
        params={"select": "id", "est_de_garde": "is.true", "limit": "5000"},
        timeout=30,
    )
    r.raise_for_status()
    return {ligne["id"] for ligne in r.json()}


def appliquer_gardes(ids_de_garde, semaine_debut, semaine_fin):
    """Ecrit la nouvelle liste de garde en 3 requetes groupees au lieu de
    deux par pharmacie (l'ancienne version faisait ~106 appels HTTP pour
    53 pharmacies, autant d'occasions d'echouer a mi-parcours).

    Ordre : activer les nouvelles, desactiver les anciennes, historique.
    Il n'y a donc jamais d'instant ou la base affiche zero pharmacie."""
    maintenant = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    if not ids_de_garde:
        return  # securite : ne jamais vider la liste (main() l'empeche deja)

    liste = ",".join(str(i) for i in ids_de_garde)

    # 1. Les nouvelles pharmacies de garde a true, en un seul appel.
    #    On fait ca EN PREMIER : l'app n'affiche jamais une liste vide,
    #    meme si le script s'interrompt entre deux etapes.
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/pharmacies",
        headers=HEADERS,
        params={"id": f"in.({liste})"},
        json={"est_de_garde": True, "derniere_maj_garde": maintenant},
        timeout=60,
    )
    r.raise_for_status()

    # 2. Puis on retire la garde a celles de la semaine precedente qui ne
    #    sont plus dans la nouvelle liste (et seulement celles-la).
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/pharmacies",
        headers=HEADERS,
        params={"est_de_garde": "is.true", "id": f"not.in.({liste})"},
        json={"est_de_garde": False},
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

        # Une semaine = UNE liste. Si le script a deja ecrit une liste
        # differente pour cette meme semaine (INAM a corrige sa page, ou
        # date estimee), on retire les pharmacies qui n'y sont plus. Sans
        # ca l'historique s'additionne (51 + 54 = 105 lignes) et fausse le
        # controle de vraisemblance. Non bloquant : les donnees de l'app
        # (est_de_garde) sont deja justes a ce stade.
        try:
            r = requests.delete(
                f"{SUPABASE_URL}/rest/v1/gardes_historique",
                headers={**HEADERS, "Prefer": "return=minimal"},
                params={
                    "semaine_debut": f"eq.{semaine_debut.isoformat()}",
                    "pharmacie_id": f"not.in.({liste})",
                },
                timeout=60,
            )
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"AVERTISSEMENT : nettoyage de l'historique impossible ({exc}).")


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
    periode_incertaine = False
    if not semaine_debut or not semaine_fin:
        print("AVERTISSEMENT : periode 'du ... au ...' introuvable sur la page.")
        print("-> Formulation probablement modifiee. On continue quand meme avec une date estimee.")
        aujourdhui = datetime.now().date()
        semaine_debut = aujourdhui - timedelta(days=aujourdhui.weekday())
        semaine_fin = semaine_debut + timedelta(days=7)  # meme style qu'INAM : lundi -> lundi suivant
        periode_incertaine = True

    dernier_debut, dernier_fin, dernier_total = derniere_semaine_en_base()
    print(f"Derniere semaine en base : {dernier_debut} -> {dernier_fin} ({dernier_total} pharmacies)")

    # Liste INAM perimee : la periode affichee est terminee et la suivante
    # n'est pas encore publiee. Ce n'est PAS une erreur (le workflow
    # repasse tout seul, y compris mercredi/jeudi), juste une trace dans
    # le journal pour savoir pourquoi rien ne bouge.
    aujourdhui_utc = datetime.now(timezone.utc).date()
    if not periode_incertaine and semaine_fin < aujourdhui_utc:
        retard = (aujourdhui_utc - semaine_fin).days
        print(f"ATTENTION : INAM affiche encore la semaine du {semaine_debut} au {semaine_fin} "
              f"(terminee depuis {retard} jour(s)). Nouvelle liste pas encore publiee.")

    # Arret precoce : rien de nouveau a faire.
    if dernier_debut == semaine_debut.isoformat() and not FORCE and not periode_incertaine:
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
    # semaine calme. La reference est la liste ACTUELLEMENT en garde dans
    # `pharmacies` (ce que voient les utilisateurs), et non le compte de
    # l'historique : celui-ci peut contenir deux listes accumulees.
    en_garde_actuel = ids_en_garde_actuels()
    reference = len(en_garde_actuel) or dernier_total
    if reference and len(entrees) < reference * FRACTION_MINIMALE:
        print(f"ERREUR : {len(entrees)} pharmacies contre {reference} actuellement de garde.")
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

    # Liste identique a celle deja en garde : rien a ecrire. Evite de refaire
    # le meme travail (et de re-logger les memes non reconnues) a chaque
    # passage horaire quand la periode n'a pas pu etre lue sur la page.
    meme_liste = set(ids_de_garde) == en_garde_actuel
    meme_semaine = dernier_debut == semaine_debut.isoformat()
    if meme_liste and not FORCE and (meme_semaine or periode_incertaine):
        print("Liste identique a celle deja en garde -> rien a faire.")
        return

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
