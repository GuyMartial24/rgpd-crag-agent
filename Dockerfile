# =====================================================================
# Image Docker de l'API RGPD CRAG Agent
#
# Construction : docker build -t rgpd-crag-agent .
# Lancement    : docker run -p 8000:8000 rgpd-crag-agent
# (en pratique, on utilise plutôt docker compose, qui démarre aussi
# Ollama et lance l'ingestion : voir docker-compose.yml)
# =====================================================================

# Image de départ : Python 3.12 en version « slim » (allégée).
FROM python:3.12-slim

# Variables d'environnement pour Python et pip :
# - PYTHONDONTWRITEBYTECODE : pas de fichiers .pyc (inutiles ici)
# - PYTHONUNBUFFERED        : les logs s'affichent immédiatement
# - PIP_NO_CACHE_DIR        : pas de cache pip, l'image est plus légère
# - PIP_DISABLE_PIP_VERSION_CHECK : pas de vérification de version pip
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dossier de travail dans le conteneur : toutes les commandes suivantes
# s'exécutent dans /app.
WORKDIR /app

# 1) On copie d'abord uniquement requirements.txt, puis on installe les
#    dépendances. Docker garde cette étape en cache : tant que
#    requirements.txt ne change pas, les dépendances ne sont pas
#    réinstallées à chaque modification du code (build plus rapide).
COPY requirements.txt .
RUN pip install -r requirements.txt

# 2) On copie ensuite le code de l'application et les PDF du RGPD.
#    (les fichiers listés dans .dockerignore ne sont pas copiés)
COPY src/ src/
COPY data/ data/

# 3) Sécurité : on évite de faire tourner l'application en « root ».
#    On crée un utilisateur « app », ainsi que le dossier de la base
#    ChromaDB dont il sera propriétaire (il doit pouvoir y écrire).
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/chroma_db \
    && chown app:app /app/chroma_db

# Toutes les commandes suivantes s'exécutent avec l'utilisateur « app ».
USER app

# Emplacement de la base vectorielle (lu par src/config.py).
# docker-compose y monte un volume pour conserver les données.
ENV CHROMA_PATH=/app/chroma_db

# Port sur lequel l'API écoute (information pour Docker).
EXPOSE 8000

# Vérification de santé : toutes les 30 secondes, Docker appelle la
# route /health. Si elle ne répond pas 3 fois de suite, le conteneur
# est marqué « unhealthy ». On utilise Python car l'image slim ne
# contient pas curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Commande lancée au démarrage du conteneur : le serveur web uvicorn
# sert l'application FastAPI définie dans src/main.py (variable « app »).
# --host 0.0.0.0 rend l'API accessible depuis l'extérieur du conteneur.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
