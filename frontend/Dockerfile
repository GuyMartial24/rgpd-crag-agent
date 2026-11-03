# =====================================================================
# Image Docker de l'interface web
#
# Les fichiers HTML, CSS et JavaScript n'ont pas besoin d'être
# compilés : on les copie simplement dans un serveur nginx.
# =====================================================================

# Image officielle nginx, version « alpine » (très légère).
FROM nginx:1.27-alpine

# Remplace la configuration par défaut par la nôtre (proxy vers l'API).
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copie les fichiers dans le dossier servi par nginx :
# - widget.js : le chatbot, intégrable sur n'importe quel site
# - bot-avatar.png : image de l'assistant (affichée par le widget)
# - index.html, page.css et page.js : la page de démonstration
COPY index.html page.css page.js widget.js bot-avatar.png /usr/share/nginx/html/

# nginx écoute sur le port 80 dans le conteneur.
EXPOSE 80

# Pas de CMD : celui de l'image nginx (démarrage du serveur) suffit.
