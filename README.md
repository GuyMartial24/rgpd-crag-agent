# rgpd-crag-agent

[![CI](https://github.com/GuyMartial24/rgpd-crag-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/GuyMartial24/rgpd-crag-agent/actions/workflows/ci.yml)

Agent **Corrective RAG (CRAG)** bilingue (français / anglais) de questions-réponses sur le
**RGPD**, construit avec
**LangGraph**, **Ollama** (LLM et embeddings 100 % locaux), **ChromaDB** et **FastAPI**,
conteneurisé avec **Docker** et testé en **CI/CD** via GitHub Actions.

## Principe

Un RAG classique génère une réponse à partir des extraits retrouvés, même lorsqu'ils sont hors
sujet. Le CRAG ajoute une étape d'**auto-correction** : chaque extrait est évalué par le LLM, et
si aucun n'est pertinent la requête est **reformulée** avec le vocabulaire du règlement avant
une nouvelle recherche. Après un nombre limité d'essais, l'agent préfère reconnaître qu'il ne
sait pas plutôt que d'halluciner.

```mermaid
flowchart LR
    S([Question]) --> R[retrieve<br/>ChromaDB]
    R --> G{grade_documents<br/>LLM évaluateur}
    G -- extraits pertinents --> GEN[generate<br/>réponse sourcée]
    G -- aucun, essais restants --> T[transform_query<br/>reformulation]
    T --> R
    G -- aucun, essais épuisés --> F[fallback<br/>« je ne sais pas »]
    GEN --> E([Réponse + articles cités])
    F --> E
```

## Structure

```
rgpd-crag-agent/
├── .github/workflows/ci.yml   # Pipeline GitHub Actions (lint, tests, build Docker)
├── data/
│   ├── RGPD_FR.pdf            # Texte officiel du RGPD en français (JO L 119 du 4.5.2016)
│   └── RGPD_EN.pdf            # Texte officiel du RGPD en anglais
├── src/
│   ├── config.py              # Paramètres (variables d'environnement / .env)
│   ├── ingest.py              # Extraction PDF, découpage par article, indexation ChromaDB
│   ├── graph.py               # Nœuds et flux de décision LangGraph (CRAG)
│   └── main.py                # API REST FastAPI
├── tests/
│   ├── test_ingest.py         # Extraction des PDF, chunking et accès ChromaDB
│   └── test_agent.py          # Graphe CRAG, API et évaluation de bout en bout
├── Dockerfile
├── docker-compose.yml         # ollama → téléchargement des modèles → ingestion → api
└── requirements.txt
```

## Démarrage rapide (Docker)

```bash
docker compose up -d --build
```

Au premier lancement, les modèles Ollama sont téléchargés (`llama3.2:3b` et `bge-m3`, ~3 Go)
puis les deux PDF sont indexés. L'API est ensuite disponible sur http://localhost:8000
(documentation interactive : http://localhost:8000/docs).

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Dans quel délai faut-il notifier une violation de données ?", "language": "fr"}'
```

```json
{
  "answer": "Le responsable du traitement doit notifier la violation à l'autorité de contrôle dans les meilleurs délais et, si possible, 72 heures au plus tard après en avoir pris connaissance (Article 33)...",
  "sources": ["Article 33", "Article 34"],
  "search_query": "Dans quel délai faut-il notifier une violation de données ?",
  "rewrites": 0
}
```

Le champ `language` (`fr` par défaut, ou `en`) choisit la version du règlement interrogée et
la langue de la réponse.

## Développement local

Prérequis : Python 3.12 et [Ollama](https://ollama.com) installé localement.

```bash
python -m venv venv
source venv/bin/activate          # Windows : venv\Scripts\activate
pip install -r requirements.txt

ollama pull llama3.2:3b
ollama pull bge-m3

python -m src.ingest              # indexe les PDF de data/ dans ./chroma_db
uvicorn src.main:app --reload
```

Après modification des PDF ou des paramètres de découpage : `python -m src.ingest --reset`.

## Configuration

Toutes les valeurs de [src/config.py](src/config.py) sont surchargeables par variable
d'environnement ou fichier `.env` :

| Variable | Défaut | Rôle |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | URL du serveur Ollama |
| `LLM_MODEL` | `llama3.2:3b` | Modèle de génération / évaluation |
| `EMBEDDING_MODEL` | `bge-m3` | Modèle d'embeddings (multilingue) |
| `CORPUS_FILES` | `{"fr": "data/RGPD_FR.pdf", "en": "data/RGPD_EN.pdf"}` | PDF à indexer, par langue (JSON) |
| `CHROMA_PATH` | `./chroma_db` | Répertoire de la base vectorielle |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1000` / `150` | Découpage des articles |
| `TOP_K` | `4` | Nombre d'extraits récupérés |
| `MAX_REWRITES` | `2` | Reformulations maximales avant abandon |

## Qualité du code

Le code suit la PEP 8 (lignes de 79 caractères maximum), vérifiée par
[ruff](https://docs.astral.sh/ruff/) avec la configuration de [pyproject.toml](pyproject.toml).
La CI exécute les mêmes commandes :

```bash
ruff check src tests          # lint (style PEP 8, imports, nommage, erreurs courantes)
ruff format src tests         # formatage automatique
ruff format --check src tests # vérification du formatage, sans modifier les fichiers
```

## Tests

```bash
python -m pytest -v
```

Les tests unitaires utilisent un LLM et des embeddings factices : ils s'exécutent sans Ollama,
y compris en CI. L'évaluation de bout en bout (questions de référence → articles attendus)
s'active avec Ollama démarré et le corpus indexé :

```bash
RUN_OLLAMA_TESTS=1 python -m pytest tests/test_agent.py -v
```

## Corpus

Le corpus est constitué des PDF officiels du règlement (UE) 2016/679 publiés au
[Journal officiel](https://eur-lex.europa.eu/eli/reg/2016/679/oj), en français et en anglais.
L'ingestion ([src/ingest.py](src/ingest.py)) :

1. extrait le texte avec `pypdf` en retirant pieds de page et notes de bas de page ;
2. découpe les **173 considérants** et les **99 articles**, sans les intitulés de chapitre et de section ;
3. découpe les sections longues en chunks qui rappellent leur article d'origine ;
4. indexe le tout dans une collection ChromaDB unique, avec la langue en métadonnée (`lang`).

> Cet outil fournit une aide à la lecture du RGPD et ne constitue pas un conseil juridique.
