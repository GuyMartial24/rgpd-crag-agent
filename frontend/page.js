// =====================================================================
// Interactions de la page de démonstration
//
// - les boutons « Discuter avec Martial » ouvrent le chatbot ;
// - les questions d'exemple sont posées directement au chatbot ;
// - le bouton « Copier » copie le code d'intégration.
//
// Le chatbot (widget.js) met à disposition window.RgpdChatWidget, avec
// deux commandes : open() et ask(question).
// =====================================================================

(function () {
    function openChat() {
        // Le widget est peut-être encore en cours de chargement.
        if (window.RgpdChatWidget) {
            window.RgpdChatWidget.open();
        }
    }

    function askChat(question) {
        if (window.RgpdChatWidget) {
            window.RgpdChatWidget.ask(question);
        }
    }

    // Boutons qui ouvrent la discussion.
    for (const button of document.querySelectorAll("[data-open-chat]")) {
        button.addEventListener("click", openChat);
    }

    // Questions d'exemple : le texte est dans l'attribut data-question.
    for (const button of document.querySelectorAll("[data-question]")) {
        button.addEventListener("click", function () {
            askChat(button.dataset.question);
        });
    }

    // Code d'intégration : on remplace l'adresse d'exemple par celle du
    // serveur qui affiche cette page (le widget est servi au même
    // endroit).
    const snippet = document.getElementById("snippet");
    snippet.textContent = snippet.textContent.replace(
        "http://localhost:8080",
        window.location.origin
    );

    // Bouton « Copier ».
    for (const button of document.querySelectorAll("[data-copy]")) {
        button.addEventListener("click", async function () {
            const code = document.getElementById(button.dataset.copy);
            try {
                await navigator.clipboard.writeText(code.textContent);
                button.textContent = "Copié !";
            } catch (error) {
                // Copie refusée par le navigateur (page non sécurisée...).
                button.textContent = "Sélectionnez le code";
            }
            setTimeout(function () {
                button.textContent = "Copier";
            }, 2000);
        });
    }
})();
