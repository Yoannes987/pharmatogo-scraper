# Scrapers PharmaTogo

Les deux robots qui alimentent la base Supabase de l'application PharmaTogo
depuis le site de l'INAM (Institut National d'Assurance Maladie du Togo).

Ils tournent tout seuls sur GitHub Actions. Il n'y a rien a faire au
quotidien : ce document sert a comprendre ce qui se passe si un jour
quelque chose casse.

---

## Les deux robots

| Fichier | Ce qu'il fait | Quand il passe |
|---|---|---|
| `scraper_gardes.py` | Liste des pharmacies de garde de la semaine | Toutes les heures, dimanche a mardi |
| `scraper_medicaments.py` | Catalogue des medicaments et leur remboursement | Le 2 de chaque mois |

Source unique : `https://www.inam.tg/`

### Secrets necessaires (Settings > Secrets and variables > Actions)

- `SUPABASE_URL` -- ex. `https://xxxxx.supabase.co`
- `SUPABASE_SERVICE_KEY` -- la cle **service_role**, pas la cle publique

---

## Le principe le plus important

**L'application ne parle JAMAIS a inam.tg. Elle ne lit que Supabase.**

```
inam.tg  --(robot, 1x/semaine ou 1x/mois)-->  Supabase  --(lecture)-->  App
```

Consequence directe : **si inam.tg tombe en panne, l'application continue de
fonctionner normalement** avec les dernieres donnees connues. La base sert de
reservoir. C'est deja arrive (panne de plusieurs jours en aout 2026) et
l'application n'a rien senti.

Le seul effet d'une panne longue, c'est que les donnees vieillissent. C'est
pour cela que l'app affiche toujours **de quand datent les donnees** ("Semaine
du 24 au 31 aout") : l'utilisateur n'est jamais trompe, meme quand le robot
est bloque.

---

## Les trois regles de securite des robots

Elles repondent toutes a la meme question : *que se passe-t-il si le robot
se trompe ?* La reponse doit toujours etre **"rien de grave, et ca se voit"**.

### 1. Echouer sans rien casser

Un robot qui plante ne doit **jamais** laisser la base dans un etat pire
qu'avant. Concretement :

- **Les gardes** : le script calcule d'abord TOUTES les correspondances, les
  verifie, et n'ecrit qu'a la fin. L'ancienne version remettait
  `est_de_garde = false` partout *avant* de commencer -- une coupure reseau
  au mauvais moment affichait "aucune pharmacie de garde" un dimanche soir.
- **Les medicaments** : c'est un *upsert*. Il ajoute et met a jour, il ne
  supprime jamais. Un medicament disparu de la page INAM reste en base.

### 2. Refuser les donnees invraisemblables

Un robot ne doit pas ecraser de bonnes donnees avec des mauvaises. Chaque
script s'arrete si le resultat est suspect :

| Situation | Reaction |
|---|---|
| Aucune periode "du ... au ..." trouvee | Arret, rien n'est ecrit |
| 0 pharmacie dans le tableau | Arret, rien n'est ecrit |
| Moitie moins de pharmacies que la semaine derniere | Arret, rien n'est ecrit |
| Aucun nom ne correspond a la base | Arret, l'ancienne liste est gardee |
| Moins de 3500 medicaments extraits | Arret, rien n'est ecrit |

Dans tous ces cas, l'app garde les donnees precedentes -- perimees mais
justes, et datees.

### 3. Ne pas travailler pour rien

Le script des gardes compare la periode affichee sur inam.tg a ce qui est
deja dans `gardes_historique`. Si c'est la meme semaine, il s'arrete apres
2 requetes. Sur les ~72 passages hebdomadaires, un seul fait le travail
complet.

---

## Comment savoir qu'un robot est en panne

GitHub envoie **un e-mail au proprietaire du depot a chaque echec** de
workflow. C'est le systeme d'alerte : pas besoin d'en construire un autre.

Onglet **Actions** du depot : croix rouge = echec, le journal explique
lequel des garde-fous ci-dessus s'est declenche.

---

## Que faire quand ca casse vraiment

Un robot depend de la structure HTML d'un site qu'on ne controle pas. Il
finira par casser un jour. Voici les cas, du plus probable au moins probable.

### inam.tg est en panne (le plus frequent)

**Ne rien faire.** Le workflow echouera, GitHub enverra un mail, et il
reessaiera au passage suivant. Quand le site revient, le robot repart tout
seul et rattrape la semaine. L'app n'a rien senti entre-temps.

### Une pharmacie de garde n'a pas ete reconnue

Le journal affiche `[NON RECONNUE] 'PHARMACIE MACHIN'` et la ligne est
enregistree dans la table `pharmacies_non_reconnues` de Supabase.

C'est presque toujours une pharmacie qui **n'existe pas dans la table
`pharmacies`** (ouverte apres l'import de la liste ONTP). Il faut l'ajouter
a la main dans Supabase, puis relancer le workflow des gardes en cochant
**force** (sinon l'arret precoce l'empechera de retravailler la semaine).

### L'INAM change la structure de sa page

Le robot s'arretera sur un des garde-fous plutot que d'ecrire n'importe
quoi. L'app continue avec les dernieres donnees valides. Il faut alors
regarder la page et adapter le script -- c'est le seul cas qui demande une
vraie intervention.

Les endroits a regarder en priorite :

- **Gardes** : `extraire_periode()` (la phrase "du X au Y mois annee") et
  `extraire_pharmacies_de_garde()` (le premier `<table>` de la page).
- **Medicaments** : `extraire_medicaments()` -- les `<td class="Nom">` et
  les `<div class="modal">`.

---

## Deux pieges deja rencontres, a ne pas re-decouvrir

**L'encodage.** Les pages d'inam.tg sont en UTF-8 mais ne le declarent pas.
Sans `resp.encoding = "utf-8"` force dans le code, la base se remplit de
`COMPRIMÃ‰S` et `DÃ©kon`. Ne pas supprimer cette ligne.

**Le HTML invalide de la page medicaments.** Elle contient des `<div>` a
l'interieur des `<tr>`, ce qui est interdit. Si on parcourt les lignes par
`<tr>`, l'analyseur en fusionne et on en perd les deux tiers (4811 lignes
trouvees au lieu de 14337). C'est pourquoi le script parcourt les
`<td class="Nom">`. Ne pas "simplifier" cette boucle.

**Bonus -- l'INAM se contredit.** La page medicaments contient 14337 lignes
pour seulement 4811 codes distincts, et 1168 de ces codes apparaissent avec
**des prix differents** (jusqu'a 6 prix pour un meme medicament). Aucune
regle ne permet de deviner le bon. Le script garde la ligne complete dont le
prix est le plus frequent (voir `choisir_prix_representatif`). C'est un choix
assume, pas un bug -- et c'est pour ca que l'app doit toujours dire de
confirmer le prix en officine.

---

## Tester sans rien casser

Les deux scripts acceptent `--dry-run` : ils font toute l'analyse, affichent
le rapport, et **n'ecrivent rien** en base.

```bash
export SUPABASE_URL="https://xxxxx.supabase.co"
export SUPABASE_SERVICE_KEY="..."

python scraper_gardes.py --dry-run
python scraper_gardes.py --force --dry-run   # ignore l'arret precoce
python scraper_medicaments.py --dry-run
```
