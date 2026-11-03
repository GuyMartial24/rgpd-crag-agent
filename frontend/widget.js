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
//   data-title    : nom de l'assistant (défaut : Martial)
//   data-role     : titre affiché sous le nom (défaut : Data & AI Engineer)
//   data-avatar   : image de l'assistant (défaut : bot-avatar.png, à côté
//                   de widget.js)
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

    // Personnalité de l'assistant : son nom et son titre.
    let title = "Martial";
    if (scriptTag.dataset.title) {
        title = scriptTag.dataset.title;
    }

    let role = "Data & AI Engineer";
    if (scriptTag.dataset.role) {
        role = scriptTag.dataset.role;
    }

    // Image de l'assistant : par défaut, bot-avatar.png dans le même
    // dossier que widget.js (même quand le widget est intégré sur un
    // autre site).
    let avatarUrl = new URL("bot-avatar.png", scriptTag.src).href;
    if (scriptTag.dataset.avatar) {
        avatarUrl = scriptTag.dataset.avatar;
    }

    // Crédit de l'image par défaut, obligatoire avec la licence gratuite
    // de Magnific (Freepik).
    const AVATAR_CREDIT_TEXT = "Image de roserodionova sur Magnific";
    const AVATAR_CREDIT_URL =
        "https://www.magnific.com/fr/vecteurs-libre/bot-mignon-dire-aux-utilisateurs-bonjour-chatbot-salue-consultation-ligne_4015765.htm";

    // Textes de l'interface dans chaque langue.
    const TEXTS = {
        fr: {
            // {name} et {role} sont remplacés par le nom et le titre.
            welcome:
                "Bonjour, je suis {name}, {role}. Je suis un assistant IA " +
                "spécialisé dans le RGPD : posez-moi vos questions, je vous " +
                "réponds à partir du texte officiel en citant les articles utilisés.",
            placeholder: "Écrivez votre question…",
            send: "Envoyer",
            sources: "Sources",
            rewrites: "Question reformulée",
            times: "fois",
            errorOllama: "Le modèle de langage est indisponible. Réessayez dans quelques instants.",
            errorTimeout:
                "La réponse prend trop de temps : le serveur est très sollicité. " +
                "Réessayez dans quelques instants, si possible avec une question plus précise.",
            errorInvalid: "La question doit contenir entre 3 et 1000 caractères.",
            errorNetwork: "Impossible de joindre le serveur. Vérifiez votre connexion.",
            errorServer: "Erreur inattendue du serveur (code ",
            disclaimer: "Aide à la lecture du RGPD, pas un conseil juridique.",
            understood: "Question comprise",
            newConversation: "Nouvelle conversation",
            // Étape en cours, affichée à côté des trois points d'attente.
            steps: {
                analyze: "Lecture de la question…",
                search: "Recherche dans le RGPD…",
                grade: "Vérification des extraits…",
                rewrite: "Reformulation de la recherche…",
                write: "Rédaction de la réponse…",
            },
        },
        en: {
            welcome:
                "Hello, I am {name}, {role}. I am an AI assistant specialised " +
                "in the GDPR: ask me your questions, I answer from the official " +
                "text and cite the articles used.",
            placeholder: "Type your question…",
            send: "Send",
            sources: "Sources",
            rewrites: "Question rephrased",
            times: "time(s)",
            errorOllama: "The language model is unavailable. Please try again shortly.",
            errorTimeout:
                "The answer is taking too long: the server is very busy. " +
                "Please try again shortly, ideally with a more specific question.",
            errorInvalid: "The question must be between 3 and 1000 characters long.",
            errorNetwork: "Unable to reach the server. Check your connection.",
            errorServer: "Unexpected server error (code ",
            disclaimer: "A reading aid for the GDPR, not legal advice.",
            understood: "Understood as",
            newConversation: "New conversation",
            steps: {
                analyze: "Reading the question…",
                search: "Searching the GDPR…",
                grade: "Checking the extracts…",
                rewrite: "Rephrasing the search…",
                write: "Writing the answer…",
            },
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

        /* Quand l'image de l'assistant est chargée, la bulle devient un
           rond blanc à bordure bleue qui affiche le robot. */
        .launcher img {
            display: none;
            width: 46px;
            height: 46px;
            object-fit: contain;
        }

        .launcher.has-avatar {
            background: #ffffff;
            border: 3px solid var(--primary);
        }

        .launcher.has-avatar:hover {
            background: #ffffff;
        }

        .launcher.has-avatar img {
            display: block;
        }

        .launcher.has-avatar svg {
            display: none;
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

        /* Rond avec l'image de l'assistant (ou son initiale si l'image
           ne peut pas être chargée). */
        .avatar {
            flex: none;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: #ffffff;
            color: var(--primary);
            font-weight: 700;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }

        .avatar img {
            width: 34px;
            height: 34px;
            object-fit: contain;
        }

        .identity {
            flex: 1;
            min-width: 0;
        }

        .header h2 {
            margin: 0;
            font-size: 16px;
            line-height: 1.2;
        }

        .role {
            margin: 0;
            font-size: 12px;
            opacity: 0.85;
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

        /* Trois points animés pendant que l'agent réfléchit, suivis de
           l'étape en cours. */
        .typing .status {
            margin-left: 6px;
            font-size: 13px;
            color: var(--text-light);
        }

        .typing .dot {
            display: inline-block;
            width: 7px;
            height: 7px;
            margin: 0 2px;
            border-radius: 50%;
            background: var(--text-light);
            animation: blink 1.2s infinite;
        }

        .typing .dot:nth-child(2) {
            animation-delay: 0.2s;
        }

        .typing .dot:nth-child(3) {
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

        /* Crédit de l'image de l'assistant, discret sous l'avertissement. */
        .credit {
            margin: 0;
            padding: 0 12px 8px;
            font-size: 10px;
            text-align: center;
            background: var(--background);
        }

        .credit[hidden] {
            display: none;
        }

        .credit a {
            color: var(--text-light);
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
            <img alt="">
        </button>

        <section class="panel" role="dialog" aria-label="Assistant RGPD" hidden>
            <header class="header">
                <div class="avatar" aria-hidden="true"></div>
                <div class="identity">
                    <h2 class="title"></h2>
                    <p class="role"></p>
                </div>
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
            <p class="credit" hidden>
                <a target="_blank" rel="noopener noreferrer"></a>
            </p>
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

        // Nom, titre et initiale de l'assistant dans l'en-tête.
        shadow.querySelector(".title").textContent = title;
        shadow.querySelector(".role").textContent = role;
        shadow.querySelector(".avatar").textContent = title.charAt(0).toUpperCase();
        loadAvatar(shadow);
        panel.setAttribute("aria-label", title + ", " + role);
        launcher.setAttribute("aria-label", "Discuter avec " + title);

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

    // ----- Image de l'assistant -----

    function loadAvatar(shadow) {
        // On charge l'image une fois : si elle est disponible, on
        // l'affiche dans la bulle et dans l'en-tête ; sinon, la bulle
        // garde son icône et l'en-tête l'initiale du nom.
        const image = new Image();

        image.onload = function () {
            launcher.querySelector("img").src = avatarUrl;
            launcher.classList.add("has-avatar");

            const headerImage = document.createElement("img");
            headerImage.src = avatarUrl;
            headerImage.alt = "";
            const avatar = shadow.querySelector(".avatar");
            avatar.textContent = "";
            avatar.appendChild(headerImage);

            // Crédit obligatoire, seulement pour l'image fournie par défaut.
            if (!scriptTag.dataset.avatar) {
                const credit = shadow.querySelector(".credit");
                const link = credit.querySelector("a");
                link.textContent = AVATAR_CREDIT_TEXT;
                link.href = AVATAR_CREDIT_URL;
                credit.hidden = false;
            }
        };

        image.src = avatarUrl;
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
        launcher.setAttribute("aria-label", "Fermer la discussion avec " + title);
        input.focus();
        // La zone des messages n'a de hauteur qu'une fois affichée : on
        // fait défiler jusqu'au dernier message à ce moment-là.
        scrollToBottom();
        saveConversation();
    }

    function closePanel() {
        panel.hidden = true;
        launcher.setAttribute("aria-label", "Discuter avec " + title);
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
        addBotMessage(welcomeText(), false);
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
        addBotMessage(welcomeText(), false);
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

    function welcomeText() {
        // Message d'accueil avec le nom et le titre de l'assistant.
        return TEXTS[currentLanguage].welcome
            .replace("{name}", title)
            .replace("{role}", role);
    }

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
        const bubble = addBotMessage(data.answer, false);
        decorateAnswer(bubble, data);
    }

    function decorateAnswer(bubble, data) {
        // Ajoute sous la réponse les sources et les précisions de l'agent.
        const texts = TEXTS[currentLanguage];

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
        // Trois points animés et l'étape en cours (HTML fixe, sans
        // donnée saisie par l'utilisateur).
        const bubble = document.createElement("div");
        bubble.className = "message bot typing";
        bubble.innerHTML =
            '<span class="dot"></span><span class="dot"></span>' +
            '<span class="dot"></span><span class="status"></span>';
        setTypingStep(bubble, "analyze");
        messages.appendChild(bubble);
        scrollToBottom();
        return bubble;
    }

    function setTypingStep(typing, step) {
        // Affiche l'étape annoncée par l'API (« search », « write »...).
        const label = TEXTS[currentLanguage].steps[step];
        if (label) {
            typing.querySelector(".status").textContent = label;
        }
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
            // /ask/stream : la réponse est envoyée au fil de sa rédaction,
            // ce qui permet de l'afficher mot à mot.
            const response = await fetch(apiUrl + "/ask/stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    question: question,
                    language: currentLanguage,
                    // Les échanges précédents : la mémoire de l'agent.
                    history: buildHistory(),
                }),
            });

            if (response.ok) {
                await readAnswerStream(response, question, typing);
            } else {
                typing.remove();
                showError(response.status);
            }
        } catch (error) {
            // fetch échoue si le serveur ne répond pas du tout (ou si la
            // connexion est coupée pendant la réponse).
            typing.remove();
            addBotMessage(texts.errorNetwork, true);
        } finally {
            // « finally » s'exécute dans tous les cas, succès ou erreur.
            setWaiting(false);
            input.focus();
        }
    }

    async function readAnswerStream(response, question, typing) {
        // La réponse arrive en plusieurs morceaux, avec une ligne de
        // JSON par événement (format « NDJSON ») :
        //   {"type": "status", "step": "search"}  étape en cours
        //   {"type": "token", "text": "..."}      morceau de la réponse
        //   {"type": "done", "answer": "...", ...} fin, résultat complet
        //   {"type": "error", "status": 504}       erreur pendant l'envoi
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        // Texte reçu mais pas encore traité (une ligne incomplète).
        let buffer = "";
        // Bulle de la réponse, créée à l'arrivée du premier morceau.
        let bubble = null;
        let answerText = "";
        let finished = false;

        while (!finished) {
            const chunk = await reader.read();
            if (chunk.done) {
                break;
            }
            buffer += decoder.decode(chunk.value, { stream: true });

            // Une ligne peut être coupée entre deux morceaux : on traite
            // les lignes complètes, la dernière attend la suite.
            const lines = buffer.split("\n");
            buffer = lines.pop();

            for (const line of lines) {
                if (line.trim() === "") {
                    continue;
                }
                const data = JSON.parse(line);

                if (data.type === "status") {
                    setTypingStep(typing, data.step);
                } else if (data.type === "token") {
                    // Premier morceau : la bulle de réponse remplace les
                    // trois points d'attente.
                    if (bubble === null) {
                        typing.remove();
                        bubble = addBotMessage("", false);
                    }
                    answerText += data.text;
                    bubble.textContent = answerText.trimStart();
                    scrollToBottom();
                } else if (data.type === "done") {
                    typing.remove();
                    if (bubble === null) {
                        bubble = addBotMessage("", false);
                    }
                    // Réponse complète (nettoyée par l'API), puis sources.
                    bubble.textContent = data.answer;
                    delete data.type;
                    // On garde la question posée avec la réponse, pour
                    // l'historique et pour un éventuel réaffichage.
                    data.question = question;
                    decorateAnswer(bubble, data);
                    // Seuls les échanges réussis rejoignent la mémoire.
                    conversation.push(data);
                    saveConversation();
                    finished = true;
                } else if (data.type === "error") {
                    typing.remove();
                    showError(data.status);
                    finished = true;
                }
            }
        }

        // Flux terminé sans événement « done » ni « error » : la
        // connexion a été coupée en cours de route.
        if (!finished) {
            typing.remove();
            addBotMessage(TEXTS[currentLanguage].errorNetwork, true);
        }
    }

    function showError(status) {
        // Message d'erreur adapté au code HTTP renvoyé par l'API.
        const texts = TEXTS[currentLanguage];
        if (status === 504) {
            // Délai dépassé (côté API ou côté nginx).
            addBotMessage(texts.errorTimeout, true);
        } else if (status === 503) {
            addBotMessage(texts.errorOllama, true);
        } else if (status === 422) {
            addBotMessage(texts.errorInvalid, true);
        } else {
            addBotMessage(texts.errorServer + status + ").", true);
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
