const express = require('express');
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode');
const path = require('path');
const fs = require('fs');

const app = express();
const port = Number(process.env.WA_PORT || 3000);
const API_KEY = process.env.WA_API_KEY || null;
const sessionDataPath = process.env.WA_SESSION_DIR
    ? path.resolve(process.env.WA_SESSION_DIR)
    : path.join(__dirname, 'whatsapp_session');
const customChromePath = process.env.CHROME_PATH || null;
const logDir = process.env.WA_LOG_DIR ? path.resolve(process.env.WA_LOG_DIR) : null;
const SERVICE_VERSION = "1.0.0";

// Ensure session directory exists
try {
    fs.mkdirSync(sessionDataPath, { recursive: true });
} catch (e) {
    // Ignore if directory exists
}

// ── Simple File & Console Logger ──────────────────────────────────────────────
function log(level, message, meta) {
    const timestamp = new Date().toISOString();
    const logLine = `[${timestamp}] [${level.toUpperCase()}] [WhatsApp]: ${message}${meta ? ' ' + JSON.stringify(meta) : ''}\n`;
    if (level === 'error') {
        console.error(logLine.trim());
    } else if (level === 'warn') {
        console.warn(logLine.trim());
    } else {
        console.log(logLine.trim());
    }

    if (logDir) {
        try {
            fs.mkdirSync(logDir, { recursive: true });
            fs.appendFileSync(path.join(logDir, 'whatsapp.log'), logLine, 'utf8');
        } catch (err) {
            // Non-fatal logging error
        }
    }
}

// ── API Key Middleware ────────────────────────────────────────────────────────
function requireApiKey(req, res, next) {
    if (!API_KEY) return next();
    const key = req.headers['x-api-key'];
    if (key === API_KEY) return next();
    return res.status(401).json({ success: false, error: 'Unauthorized' });
}

app.use(express.json({ limit: '100mb' }));

// ── State Machine & Client Lifecycle Management ──────────────────────────────
// States: STOPPED, STARTING, QR_REQUIRED, AUTHENTICATED, READY, DISCONNECTED, RESTART_WAIT, ERROR, SHUTTING_DOWN
let clientStatus = 'STOPPED';
let qrCodeData = null;
let clientInfo = null;
let client = null;
let restartCount = 0;
let restartTimer = null;
let isExecutingAction = false;
const actionQueue = [];

function setStatus(newStatus) {
    const prevStatus = clientStatus;
    clientStatus = newStatus;
    log('info', `State changed: ${prevStatus} -> ${newStatus}`);
}

/**
 * Serialized async action runner ensuring createClient, destroyClient,
 * and restartClient never run concurrently or race against each other.
 */
function enqueueAction(actionFn) {
    return new Promise((resolve, reject) => {
        actionQueue.push({ actionFn, resolve, reject });
        processNextAction();
    });
}

async function processNextAction() {
    if (isExecutingAction || actionQueue.length === 0) return;
    isExecutingAction = true;
    const { actionFn, resolve, reject } = actionQueue.shift();
    try {
        const result = await actionFn();
        resolve(result);
    } catch (err) {
        log('error', `Action execution error: ${err.message}`, { stack: err.stack });
        reject(err);
    } finally {
        isExecutingAction = false;
        setImmediate(processNextAction);
    }
}

/**
 * Remove stale Chrome lock/port files left behind by unclean shutdowns.
 * Puppeteer will refuse to launch if any of these are present.
 */
function cleanStaleBrowserLocks() {
    const STALE_FILES = ['SingletonLock', 'SingletonCookie', 'DevToolsActivePort'];
    try {
        const sessionPath = path.join(sessionDataPath, 'session');
        if (fs.existsSync(sessionPath)) {
            for (const fileName of STALE_FILES) {
                const filePath = path.join(sessionPath, fileName);
                if (fs.existsSync(filePath)) {
                    log('info', `Removing stale ${fileName} at ${filePath}`);
                    fs.unlinkSync(filePath);
                }
            }
        }
    } catch (e) {
        log('warn', `Could not clean lock files: ${e.message}`);
    }
}

/**
 * Destroy existing client instance and browser safely.
 */
async function destroyCurrentClient() {
    if (restartTimer) {
        clearTimeout(restartTimer);
        restartTimer = null;
    }

    if (!client) {
        return;
    }

    log('info', 'Destroying existing WhatsApp client and releasing browser...');
    const oldClient = client;
    client = null;

    try {
        oldClient.removeAllListeners();
        await oldClient.destroy();
        log('info', 'WhatsApp client destroyed successfully.');
    } catch (err) {
        log('warn', `Error while destroying WhatsApp client: ${err.message}`);
    }

    // Allow OS to release file handles and locks
    await new Promise((r) => setTimeout(r, 800));
    cleanStaleBrowserLocks();
}

/**
 * Instantiate and initialize a new WhatsApp Client.
 */
async function initializeNewClient() {
    if (clientStatus === 'SHUTTING_DOWN') return;

    await destroyCurrentClient();
    cleanStaleBrowserLocks();

    setStatus('STARTING');
    qrCodeData = null;
    clientInfo = null;

    const puppeteerArgs = [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-accelerated-2d-canvas',
        '--no-first-run',
        '--no-zygote',
        '--disable-gpu'
    ];

    const puppeteerConfig = {
        headless: true,
        args: puppeteerArgs
    };

    if (customChromePath && fs.existsSync(customChromePath)) {
        log('info', `Using custom Chromium binary at: ${customChromePath}`);
        puppeteerConfig.executablePath = customChromePath;
    }

    log('info', `Initializing WhatsApp Client with session directory: ${sessionDataPath}`);

    const newClient = new Client({
        authStrategy: new LocalAuth({
            dataPath: sessionDataPath
        }),
        puppeteer: puppeteerConfig
    });

    newClient.on('qr', async (qr) => {
        setStatus('QR_RECEIVED');
        log('info', 'QR Code received. Please scan via the School ERP dashboard.');
        try {
            qrCodeData = await qrcode.toDataURL(qr);
        } catch (err) {
            log('error', 'Failed to generate QR data URL', err);
        }
    });

    newClient.on('authenticated', () => {
        setStatus('AUTHENTICATED');
        qrCodeData = null;
        restartCount = 0; // Reset restart counter on successful auth
        log('info', 'Authenticated successfully with WhatsApp.');
    });

    newClient.on('auth_failure', (msg) => {
        setStatus('AUTHENTICATION_FAILED');
        log('error', `WhatsApp authentication failed: ${msg}`);
        scheduleRestart(5000);
    });

    newClient.on('ready', () => {
        setStatus('READY');
        clientInfo = newClient.info;
        restartCount = 0;
        log('info', 'WhatsApp Client is fully READY for messaging.');
    });

    newClient.on('disconnected', (reason) => {
        if (clientStatus === 'SHUTTING_DOWN') return;
        setStatus('DISCONNECTED');
        log('warn', `WhatsApp client disconnected. Reason: ${reason}`);
        scheduleRestart(3000);
    });

    client = newClient;

    try {
        await client.initialize();
    } catch (err) {
        log('error', `Failed to initialize WhatsApp client: ${err.message}`);
        setStatus('ERROR');
        scheduleRestart(5000);
    }
}

/**
 * Schedule a restart with exponential backoff to avoid rapid crash loops.
 */
function scheduleRestart(delayMs = 3000) {
    if (clientStatus === 'SHUTTING_DOWN') return;
    if (restartTimer) {
        clearTimeout(restartTimer);
    }

    restartCount += 1;
    // Calculate backoff: min(delayMs * 1.5^(restartCount - 1), 30000)
    const backoff = Math.min(Math.round(delayMs * Math.pow(1.5, Math.max(0, restartCount - 1))), 30000);
    setStatus('RESTART_WAIT');
    log('info', `Scheduling client restart in ${backoff}ms (attempt #${restartCount})...`);

    restartTimer = setTimeout(() => {
        restartTimer = null;
        enqueueAction(initializeNewClient).catch((err) => {
            log('error', `Scheduled restart failed: ${err.message}`);
        });
    }, backoff);
}

// ── Graceful Shutdown ────────────────────────────────────────────────────────
let isShuttingDown = false;

async function handleShutdown() {
    if (isShuttingDown) return;
    isShuttingDown = true;
    setStatus('SHUTTING_DOWN');
    log('info', 'Received shutdown signal. Gracefully closing WhatsApp service...');

    if (restartTimer) {
        clearTimeout(restartTimer);
        restartTimer = null;
    }

    try {
        await destroyCurrentClient();
    } catch (err) {
        log('error', `Error during client teardown: ${err.message}`);
    }

    log('info', 'WhatsApp service shutdown complete.');
    process.exit(0);
}

process.on('SIGTERM', handleShutdown);
process.on('SIGINT', handleShutdown);

// ── Initial Startup ──────────────────────────────────────────────────────────
enqueueAction(initializeNewClient).catch((err) => {
    log('error', `Initial client creation failed: ${err.message}`);
});

// ── API Endpoints ────────────────────────────────────────────────────────────

// GET /status — Returns structured service and client state
app.get('/status', (req, res) => {
    res.json({
        service: 'running',
        status: clientStatus,
        qr: qrCodeData,
        info: clientInfo,
        version: SERVICE_VERSION,
        uptime: process.uptime(),
        restartCount: restartCount
    });
});

// POST /send — Send message or media attachment
app.post('/send', requireApiKey, async (req, res) => {
    const { phone, message = '', attachments } = req.body;
    const hasAttachments = Array.isArray(attachments) && attachments.length > 0;

    if (!phone || (!message && !hasAttachments)) {
        return res.status(400).json({ success: false, error: 'Phone and message or attachments are required' });
    }

    if (clientStatus !== 'READY' || !client) {
        return res.status(503).json({
            success: false,
            error: 'WhatsApp client is not ready. Current status: ' + clientStatus
        });
    }

    try {
        let cleanedPhone = phone.replace(/\D/g, '');

        // Morocco standard format conversion
        if (/^(06|07|05)/.test(cleanedPhone) && cleanedPhone.length === 10) {
            cleanedPhone = '212' + cleanedPhone.substring(1);
        } else if (/^(6|7|5)/.test(cleanedPhone) && cleanedPhone.length === 9) {
            cleanedPhone = '212' + cleanedPhone;
        } else if (cleanedPhone.startsWith('00212')) {
            cleanedPhone = cleanedPhone.substring(2);
        }

        const chatId = `${cleanedPhone}@c.us`;
        log('info', `Attempting to send message to: ${chatId}`);

        const isRegistered = await client.isRegisteredUser(chatId);
        if (!isRegistered) {
            log('warn', `Number ${chatId} is not registered on WhatsApp`);
            return res.status(400).json({
                success: false,
                error: `Le numéro ${phone} n'est pas enregistré sur WhatsApp.`
            });
        }

        let lastResponse = null;
        if (hasAttachments) {
            for (let i = 0; i < attachments.length; i++) {
                const attachment = attachments[i];
                const { name, mime_type, data } = attachment;
                if (!name || !data) continue;

                const media = new MessageMedia(mime_type || 'application/octet-stream', data, name);
                const options = {};
                if (message && i === 0) {
                    options.caption = message;
                }

                lastResponse = await client.sendMessage(chatId, media, options);
                log('info', `Attachment sent to ${chatId}: ${name}`);
            }

            res.json({ success: true, messageId: lastResponse?.id?.id || null });
        } else {
            lastResponse = await client.sendMessage(chatId, message);
            log('info', `Message successfully sent to ${chatId}.`);
            res.json({ success: true, messageId: lastResponse?.id?.id || null });
        }
    } catch (error) {
        log('error', `Failed to send message: ${error.message}`);
        res.status(500).json({ success: false, error: error.message });
    }
});

// POST /logout — Disconnect session and prepare fresh client
app.post('/logout', requireApiKey, async (req, res) => {
    try {
        log('info', 'Logging out from WhatsApp session via API...');
        if (client) {
            try {
                await client.logout();
            } catch (err) {
                log('warn', `Logout error on client: ${err.message}`);
            }
        }
        res.json({ success: true, message: 'Logged out successfully' });
        // Re-initialize a clean client after logout
        enqueueAction(initializeNewClient).catch((err) => {
            log('error', `Re-init after logout failed: ${err.message}`);
        });
    } catch (error) {
        log('error', `Logout endpoint error: ${error.message}`);
        res.status(500).json({ success: false, error: error.message });
    }
});

// POST /restart — Manually restart the WhatsApp client without restarting the process
app.post('/restart', requireApiKey, async (req, res) => {
    log('info', 'Manual restart requested via API...');
    res.json({ success: true, message: 'Restart initiated' });
    enqueueAction(initializeNewClient).catch((err) => {
        log('error', `Manual restart failed: ${err.message}`);
    });
});

app.listen(port, '127.0.0.1', () => {
    log('info', `WhatsApp automation service listening at http://127.0.0.1:${port}`);
    if (API_KEY) {
        log('info', 'API key authentication is ENABLED.');
    } else {
        log('info', 'API key authentication is DISABLED (set WA_API_KEY env var to enable).');
    }
});