// =====================================================================
// Widget de chat « Assistant RGPD »
//
// Ce fichier ajoute une bulle de discussion en bas à droite de
// n'importe quelle page web. Pour l'intégrer sur un site, il suffit
// d'ajouter cette ligne avant </body> :
//
//   <script src="http://localhost:8080/widget.js"></script>
//
// Options facultatives (attributs data-* de la balise <script>) :
//   data-api-url  : adresse de l'API (par défaut : même serveur que
//                   widget.js, suivi de /api)
//   data-language : langue par défaut, « fr » ou « en » (défaut : fr)
//   data-title    : titre affiché dans la fenêtre de chat
//
// Le widget utilise un « Shadow DOM » : ses styles sont isolés de ceux
// du site hôte (ils ne s'influencent pas mutuellement).
// =====================================================================

// Tout le code est placé dans une fonction exécutée immédiatement :
// ses variables ne se mélangent pas avec celles du site hôte.
(function () {
    // document.currentScript n'est disponible que pendant le chargement
    // du script : on le lit tout de suite.
    const scriptTag = document.currentScript;
    const scriptUrl = new URL(scriptTag.src, window.location.href);

    // ----- Configuration -----
    let apiUrl = scriptUrl.origin + "/api";
    if (scriptTag.dataset.apiUrl) {
        apiUrl = scriptTag.dataset.apiUrl;
    }

    let currentLanguage = "fr";
    if (scriptTag.dataset.language === "en") {
        currentLanguage = "en";
    }

    let title = "Assistant RGPD";
    if (scriptTag.dataset.title) {
        title = scriptTag.dataset.title;
    }

    // Textes de l'interface dans chaque langue.
    const TEXTS = {
        fr: {
            welcome:
                "Bonjour ! Posez-moi une question sur le RGPD. " +
                "Je réponds à partir du texte officiel et je cite les articles utilisés.",
            placeholder: "Écrivez votre question…",
            send: "Envoyer",
            sources: "Sources",
            rewrites: "Question reformulée",
            times: "fois",
            errorOllama: "Le modèle de langage est indisponible. Réessayez dans quelques instants.",
            errorInvalid: "La question doit contenir entre 3 et 1000 caractères.",
            errorNetwork: "Impossible de joindre le serveur. Vérifiez votre connexion.",
            errorServer: "Erreur inattendue du serveur (code ",
            disclaimer: "Aide à la lecture du RGPD, pas un conseil juridique.",
            understood: "Question comprise",
            newConversation: "Nouvelle conversation",
        },
        en: {
            welcome:
                "Hello! Ask me a question about the GDPR. " +
                "I answer from the official text and cite the articles used.",
            placeholder: "Type your question…",
            send: "Send",
            sources: "Sources",
            rewrites: "Question rephrased",
            times: "time(s)",
            errorOllama: "The language model is unavailable. Please try again shortly.",
            errorInvalid: "The question must be between 3 and 1000 characters long.",
            errorNetwork: "Unable to reach the server. Check your connection.",
            errorServer: "Unexpected server error (code ",
            disclaimer: "A reading aid for the GDPR, not legal advice.",
            understood: "Understood as",
            newConversation: "New conversation",
        },
    };

    // ----- Styles du widget (isolés grâce au Shadow DOM) -----
    const STYLES = `
        :host {
            --primary: #1d4ed8;
            --primary-hover: #1e40af;
            --background: #ffffff;
            --surface: #f3f4f6;
            --text: #1f2933;
            --text-light: #6b7280;
            --border: #e5e7eb;
            --error: #b91c1c;
            font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
            font-size: 15px;
            line-height: 1.5;
        }

        /* Mode sombre si le système de l'utilisateur est en sombre. */
        @media (prefers-color-scheme: dark) {
            :host {
                --primary: #3b82f6;
                --primary-hover: #2563eb;
                --background: #1f2937;
                --surface: #111827;
                --text: #f3f4f6;
                --text-light: #9ca3af;
                --border: #374151;
                --error: #f87171;
            }
        }

        * {
            box-sizing: border-box;
        }

        /* Bouton rond flottant en bas à droite. */
        .launcher {
            position: fixed;
            right: 20px;
            bottom: 20px;
            width: 60px;
            height: 60px;
            border: none;
            border-radius: 50%;
            background: var(--primary);
            color: #ffffff;
            cursor: pointer;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
            display: flex;
            align-items: center;
            justify-content: center;
            transition: transform 0.2s;
        }

        .launcher:hover {
            transform: scale(1.06);
            background: var(--primary-hover);
        }

        .launcher svg {
            width: 28px;
            height: 28px;
        }

        /* Fenêtre de discussion. */
        .panel {
            position: fixed;
            right: 20px;
            bottom: 92px;
            width: 380px;
            height: 560px;
            max-height: calc(100vh - 112px);
            display: flex;
            flex-direction: column;
            background: var(--background);
            color: var(--text);
            border: 1px solid var(--border);
            border-radius: 16px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
            overflow: hidden;
        }

        .panel[hidden] {
            display: none;
        }

        .header {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 12px 16px;
            background: var(--primary);
            color: #ffffff;
        }

        .header h2 {
            flex: 1;
            margin: 0;
            font-size: 16px;
        }

        .header select {
            font: inherit;
            font-size: 13px;
            padding: 2px 4px;
            border: none;
            border-radius: 6px;
        }

        .close,
        .reset {
            border: none;
            background: transparent;
            color: #ffffff;
            font-size: 22px;
            line-height: 1;
            cursor: pointer;
        }

        .reset {
            font-size: 18px;
        }

        /* Zone des messages, avec défilement vertical. */
        .messages {
            flex: 1;
            overflow-y: auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            background: var(--surface);
        }

        .message {
            max-width: 85%;
            padding: 10px 14px;
            border-radius: 14px;
            white-space: pre-wrap;
            word-wrap: break-word;
        }

        .message.user {
            align-self: flex-end;
            background: var(--primary);
            color: #ffffff;
            border-bottom-right-radius: 4px;
        }

        .message.bot {
            align-self: flex-start;
            background: var(--background);
            border: 1px solid var(--border);
            border-bottom-left-radius: 4px;
        }

        .message.error {
            color: var(--error);
        }

        .sources {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-top: 8px;
            white-space: normal;
        }

        .sources span {
            padding: 1px 8px;
            border-radius: 999px;
            border: 1px solid var(--border);
            font-size: 12px;
        }

        .details {
            margin-top: 6px;
            font-size: 12px;
            color: var(--text-light);
            white-space: normal;
        }

        /* Trois points animés pendant que l'agent réfléchit. */
        .typing span {
            display: inline-block;
            width: 7px;
            height: 7px;
            margin: 0 2px;
            border-radius: 50%;
            background: var(--text-light);
            animation: blink 1.2s infinite;
        }

        .typing span:nth-child(2) {
            animation-delay: 0.2s;
        }

        .typing span:nth-child(3) {
            animation-delay: 0.4s;
        }

        @keyframes blink {
            0%, 80%, 100% { opacity: 0.2; }
            40% { opacity: 1; }
        }

        /* Zone de saisie en bas de la fenêtre. */
        .composer {
            display: flex;
            gap: 8px;
            padding: 12px;
            border-top: 1px solid var(--border);
            background: var(--background);
        }

        .composer textarea {
            flex: 1;
            resize: none;
            font: inherit;
            padding: 8px 10px;
            color: var(--text);
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 10px;
        }

        .composer button {
            padding: 0 16px;
            border: none;
            border-radius: 10px;
            background: var(--primary);
            color: #ffffff;
            font: inherit;
            font-weight: 600;
            cursor: pointer;
        }

        .composer button:disabled {
            opacity: 0.6;
            cursor: wait;
        }

        .disclaimer {
            margin: 0;
            padding: 0 12px 8px;
            font-size: 11px;
            text-align: center;
            color: var(--text-light);
            background: var(--background);
        }

        /* Sur mobile, la fenêtre occupe tout l'écran. */
        @media (max-width: 480px) {
            .panel {
                right: 0;
                bottom: 0;
                width: 100%;
                height: 100%;
                max-height: 100%;
                border-radius: 0;
            }
        }
    `;

    // ----- Structure HTML du widget -----
    // Ce HTML est fixe (aucune donnée saisie par l'utilisateur) : on peut
    // l'insérer avec innerHTML sans risque.
    const TEMPLATE = `
        <button class="launcher" type="button" aria-label="Ouvrir l'assistant RGPD">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
        </button>

        <section class="panel" role="dialog" aria-label="Assistant RGPD" hidden>
            <header class="header">
                <h2 class="title"></h2>
                <select class="language" aria-label="Langue du RGPD">
                    <option value="fr">FR</option>
                    <option value="en">EN</option>
                </select>
                <button class="reset" type="button">↺</button>
                <button class="close" type="button" aria-label="Fermer">×</button>
            </header>

            <div class="messages" aria-live="polite"></div>

            <form class="composer">
                <textarea rows="2" maxlength="1000" aria-label="Votre question"></textarea>
                <button type="submit"></button>
            </form>
            <p class="disclaimer"></p>
        </section>
    `;

    // ----- Mémoire de conversation -----
    // Chaque échange réussi est gardé dans « conversation » :
    //   { question, answer, sources, standalone_question, search_query,
    //     rewrites }
    // Les derniers échanges sont renvoyés à l'API avec chaque question :
    // c'est ce qui permet de poser des questions de relance
    // (« et pour les mineurs ? »).
    let conversation = [];

    // Nombre d'échanges passés envoyés à l'API.
    const MAX_HISTORY = 6;
    // Taille maximale d'une réponse acceptée par l'API dans l'historique.
    const MAX_ANSWER_LENGTH = 5000;
    // Clé utilisée pour sauvegarder la conversation dans le navigateur.
    const STORAGE_KEY = "rgpd-chat-widget";

    // Variables remplies par createWidget().
    let launcher;
    let panel;
    let messages;
    let form;
    let input;
    let sendButton;
    let resetButton;
    let languageSelect;
    let disclaimer;
    let isWaiting = false;

    // ----- Création du widget dans la page -----

    function createWidget() {
        // Élément « hôte » ajouté à la page, qui contient le Shadow DOM.
        const host = document.createElement("div");
        host.id = "rgpd-chat-widget";
        // Toujours au premier plan, au-dessus du contenu du site.
        host.style.position = "relative";
        host.style.zIndex = "2147483000";
        document.body.appendChild(host);

        const shadow = host.attachShadow({ mode: "open" });
        shadow.innerHTML = "<style>" + STYLES + "</style>" + TEMPLATE;

        // Récupération des éléments du widget.
        launcher = shadow.querySelector(".launcher");
        panel = shadow.querySelector(".panel");
        messages = shadow.querySelector(".messages");
        form = shadow.querySelector(".composer");
        input = shadow.querySelector(".composer textarea");
        sendButton = shadow.querySelector(".composer button");
        languageSelect = shadow.querySelector(".language");
        resetButton = shadow.querySelector(".reset");
        disclaimer = shadow.querySelector(".disclaimer");

        shadow.querySelector(".title").textContent = title;

        // On reprend la conversation en cours si le visiteur a changé de
        // page sur le site (sinon, on affiche le message d'accueil).
        restoreConversation();

        // Branchement des événements.
        launcher.addEventListener("click", togglePanel);
        shadow.querySelector(".close").addEventListener("click", closePanel);
        resetButton.addEventListener("click", resetConversation);
        form.addEventListener("submit", sendQuestion);
        languageSelect.addEventListener("change", changeLanguage);

        input.addEventListener("keydown", function (event) {
            // Entrée envoie la question ; Maj+Entrée fait un retour à la
            // ligne.
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                form.requestSubmit();
            }
            // Échap ferme la fenêtre.
            if (event.key === "Escape") {
                closePanel();
            }
        });
    }

    // ----- Ouverture et fermeture -----

    function togglePanel() {
        if (panel.hidden) {
            openPanel();
        } else {
            closePanel();
        }
    }

    function openPanel() {
        panel.hidden = false;
        launcher.setAttribute("aria-label", "Fermer l'assistant RGPD");
        input.focus();
        // La zone des messages n'a de hauteur qu'une fois affichée : on
        // fait défiler jusqu'au dernier message à ce moment-là.
        scrollToBottom();
        saveConversation();
    }

    function closePanel() {
        panel.hidden = true;
        launcher.setAttribute("aria-label", "Ouvrir l'assistant RGPD");
        launcher.focus();
        saveConversation();
    }

    // ----- Langue -----

    function changeLanguage() {
        currentLanguage = languageSelect.value;
        updateTexts();
        saveConversation();
    }

    function updateTexts() {
        const texts = TEXTS[currentLanguage];
        input.placeholder = texts.placeholder;
        sendButton.textContent = texts.send;
        disclaimer.textContent = texts.disclaimer;
        resetButton.title = texts.newConversation;
        resetButton.setAttribute("aria-label", texts.newConversation);
    }

    // ----- Sauvegarde de la conversation -----
    // sessionStorage garde les données tant que l'onglet est ouvert :
    // la conversation survit au changement de page sur le site, mais
    // disparaît à la fermeture de l'onglet.
    // Les appels sont dans des try/catch car le stockage peut être
    // bloqué par le navigateur (navigation privée, réglages...) : le
    // widget doit alors fonctionner quand même, sans sauvegarde.

    function saveConversation() {
        const data = {
            conversation: conversation,
            language: currentLanguage,
            isOpen: !panel.hidden,
        };
        try {
            sessionStorage.setItem(STORAGE_KEY, JSON.stringify(data));
        } catch (error) {
            // Stockage indisponible : on continue sans sauvegarder.
        }
    }

    function restoreConversation() {
        let data = null;
        try {
            data = JSON.parse(sessionStorage.getItem(STORAGE_KEY));
        } catch (error) {
            data = null;
        }

        if (data !== null && Array.isArray(data.conversation)) {
            // On ne garde que les échanges complets (données sauvegardées
            // abîmées ou d'une ancienne version du widget).
            conversation = [];
            for (const exchange of data.conversation) {
                if (
                    exchange !== null &&
                    typeof exchange.question === "string" &&
                    typeof exchange.answer === "string" &&
                    Array.isArray(exchange.sources)
                ) {
                    conversation.push(exchange);
                }
            }
            if (data.language === "fr" || data.language === "en") {
                currentLanguage = data.language;
            }
        }

        languageSelect.value = currentLanguage;
        updateTexts();

        // Réaffichage des messages : accueil puis chaque échange.
        addBotMessage(TEXTS[currentLanguage].welcome, false);
        for (const exchange of conversation) {
            addUserMessage(exchange.question);
            addBotAnswer(exchange);
        }

        if (data !== null && data.isOpen) {
            panel.hidden = false;
            scrollToBottom();
        }
    }

    function resetConversation() {
        conversation = [];
        messages.innerHTML = "";
        addBotMessage(TEXTS[currentLanguage].welcome, false);
        saveConversation();
        input.focus();
    }

    function buildHistory() {
        // On envoie seulement les derniers échanges, au format attendu
        // par l'API : [{ question, answer }, ...].
        const recentExchanges = conversation.slice(-MAX_HISTORY);
        const history = [];
        for (const exchange of recentExchanges) {
            history.push({
                question: exchange.question,
                answer: exchange.answer.slice(0, MAX_ANSWER_LENGTH),
            });
        }
        return history;
    }

    // ----- Affichage des messages -----

    function addUserMessage(text) {
        const bubble = document.createElement("div");
        bubble.className = "message user";
        // textContent (et non innerHTML) : le texte est affiché tel quel,
        // sans être interprété comme du HTML (protection contre les
        // injections de code).
        bubble.textContent = text;
        messages.appendChild(bubble);
        scrollToBottom();
    }

    function addBotMessage(text, isError) {
        const bubble = document.createElement("div");
        bubble.className = "message bot";
        if (isError) {
            bubble.className = "message bot error";
        }
        bubble.textContent = text;
        messages.appendChild(bubble);
        scrollToBottom();
        return bubble;
    }

    function addBotAnswer(data) {
        const texts = TEXTS[currentLanguage];
        const bubble = addBotMessage(data.answer, false);

        // Étiquettes des articles utilisés.
        if (data.sources.length > 0) {
            const sourcesBox = document.createElement("div");
            sourcesBox.className = "sources";
            for (const source of data.sources) {
                const tag = document.createElement("span");
                tag.textContent = source;
                sourcesBox.appendChild(tag);
            }
            bubble.appendChild(sourcesBox);
        }

        // Question de relance : on montre comment l'agent l'a comprise
        // grâce à la conversation.
        if (data.standalone_question && data.standalone_question !== data.question) {
            addDetails(bubble, texts.understood + " : « " + data.standalone_question + " »");
        }

        // Indique si l'agent CRAG a dû reformuler la question.
        if (data.rewrites > 0) {
            addDetails(
                bubble,
                texts.rewrites + " " + data.rewrites + " " + texts.times + " : « " +
                    data.search_query + " »"
            );
        }
        scrollToBottom();
    }

    function addDetails(bubble, text) {
        const details = document.createElement("div");
        details.className = "details";
        details.textContent = text;
        bubble.appendChild(details);
    }

    function showTyping() {
        const bubble = document.createElement("div");
        bubble.className = "message bot typing";
        bubble.innerHTML = "<span></span><span></span><span></span>";
        messages.appendChild(bubble);
        scrollToBottom();
        return bubble;
    }

    function scrollToBottom() {
        messages.scrollTop = messages.scrollHeight;
    }

    // ----- Envoi d'une question à l'API -----

    async function sendQuestion(event) {
        // Empêche le rechargement de la page à l'envoi du formulaire.
        event.preventDefault();

        const question = input.value.trim();
        const texts = TEXTS[currentLanguage];

        // On ignore les envois vides ou pendant une réponse en cours.
        if (question === "" || isWaiting) {
            return;
        }
        if (question.length < 3) {
            addBotMessage(texts.errorInvalid, true);
            return;
        }

        addUserMessage(question);
        input.value = "";
        setWaiting(true);
        const typing = showTyping();

        try {
            const response = await fetch(apiUrl + "/ask", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    question: question,
                    language: currentLanguage,
                    // Les échanges précédents : la mémoire de l'agent.
                    history: buildHistory(),
                }),
            });

            typing.remove();

            if (response.ok) {
                const data = await response.json();
                // On garde la question posée avec la réponse, pour
                // l'historique et pour un éventuel réaffichage.
                data.question = question;
                addBotAnswer(data);
                // Seuls les échanges réussis rejoignent la mémoire.
                conversation.push(data);
                saveConversation();
            } else if (response.status === 503) {
                addBotMessage(texts.errorOllama, true);
            } else if (response.status === 422) {
                addBotMessage(texts.errorInvalid, true);
            } else {
                addBotMessage(texts.errorServer + response.status + ").", true);
            }
        } catch (error) {
            // fetch échoue si le serveur ne répond pas du tout.
            typing.remove();
            addBotMessage(texts.errorNetwork, true);
        } finally {
            // « finally » s'exécute dans tous les cas, succès ou erreur.
            setWaiting(false);
            input.focus();
        }
    }

    function setWaiting(waiting) {
        isWaiting = waiting;
        sendButton.disabled = waiting;
    }

    // ----- Démarrage -----
    // Si la page n'a pas fini de se charger, on attend qu'elle soit prête
    // (document.body doit exister pour y ajouter le widget).
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", createWidget);
    } else {
        createWidget();
    }
})();
