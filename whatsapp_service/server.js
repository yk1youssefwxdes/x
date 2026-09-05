const express = require('express');
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode');
const path = require('path');
const fs = require('fs');
const net = require('net');
const readline = require('readline');
const { execSync, execFileSync } = require('child_process');

// ── Configuration & Environment Defaults ──────────────────────────────────────
const SERVICE_VERSION = '1.4.0';
const port = Number(process.env.WA_PORT || 3000);
const host = process.env.WA_HOST || '127.0.0.1';
const API_KEY = process.env.WA_API_KEY || null;

const sessionDataPath = process.env.WA_SESSION_DIR
    ? path.resolve(process.env.WA_SESSION_DIR)
    : path.join(__dirname, 'whatsapp_session');

const logDir = process.env.WA_LOG_DIR
    ? path.resolve(process.env.WA_LOG_DIR)
    : path.join(__dirname, 'logs');

const INIT_TIMEOUT_MS = Number(process.env.WA_INIT_TIMEOUT_MS || 120000);           // 120 seconds
const STARTUP_DELAY_MS = Number(process.env.WA_STARTUP_DELAY_MS !== undefined ? process.env.WA_STARTUP_DELAY_MS : 300); // Fast startup delay (300ms)
const DESTROY_TIMEOUT_MS = Number(process.env.WA_DESTROY_TIMEOUT_MS || 15000);       // 15 seconds
const RESTART_MAX_DELAY_MS = Number(process.env.WA_RESTART_MAX_DELAY_MS || 60000);   // 60 seconds max backoff
const AUTH_TO_READY_TIMEOUT_MS = Number(process.env.WA_AUTH_TO_READY_TIMEOUT_MS || 60000); // 60 seconds
const MAX_ATTACHMENT_SIZE_MB = Number(process.env.WA_MAX_ATTACHMENT_SIZE_MB || 25);
const MAX_LOG_SIZE_BYTES = 10 * 1024 * 1024; // 10MB per log file before rotation

// ── Chrome Path Resolution ───────────────────────────────────────────────────
function resolveChromePath() {
    const specified = process.env.CHROME_PATH;
    if (specified) {
        const resolved = path.resolve(specified);
        if (fs.existsSync(resolved)) {
            return resolved;
        } else {
            console.warn(`[CONFIG] WARNING: CHROME_PATH "${specified}" does not exist. Falling back to Puppeteer bundled browser.`);
            return null;
        }
    }
    // Default: allow Puppeteer to use its configured/bundled browser
    return null;
}

const customChromePath = resolveChromePath();

// ── Windows Process Management & Safe Ownership Verification ──────────────────
/**
 * Safely extracts the ChildProcess object from a Client instance.
 */
function getBrowserProcess(clientInstance) {
    try {
        if (!clientInstance) return null;
        if (clientInstance.pupBrowser && typeof clientInstance.pupBrowser.process === 'function') {
            return clientInstance.pupBrowser.process() || null;
        }
        return null;
    } catch (_) {
        return null;
    }
}

/**
 * Safely extracts the OS Process ID from a Client instance.
 */
function getBrowserPid(clientInstance) {
    try {
        const proc = getBrowserProcess(clientInstance);
        return proc?.pid || null;
    } catch (_) {
        return null;
    }
}

/**
 * Test whether a process ID is currently alive on the operating system.
 */
function isProcessAlive(pid) {
    if (!pid || typeof pid !== 'number' || pid <= 0) return false;
    try {
        process.kill(pid, 0);
        return true;
    } catch (err) {
        return err.code === 'EPERM'; // Process exists but lacks signaling permission
    }
}

/**
 * Query process executable name and full command line via Windows CIM/WMI.
 */
function getProcessInfo(pid) {
    if (!pid || typeof pid !== 'number' || pid <= 0) return null;
    if (process.platform !== 'win32') return null;

    try {
        const raw = execFileSync('powershell.exe', [
            '-NoProfile',
            '-NonInteractive',
            '-Command',
            `$p = Get-CimInstance Win32_Process -Filter "ProcessId = ${pid}"; if ($p) { @{Name=$p.Name; CommandLine=$p.CommandLine} | ConvertTo-Json -Compress }`
        ], { encoding: 'utf8', timeout: 4000 }).trim();

        if (!raw) return null;
        return JSON.parse(raw);
    } catch (_) {
        return null;
    }
}

/**
 * Verify whether a process belongs specifically to THIS WhatsApp service instance.
 * Checks:
 * 1. Process exists and is alive.
 * 2. Executable is chrome.exe, msedge.exe, or chromium.exe.
 * 3. Process command line explicitly points to our configured WA_SESSION_DIR.
 */
function isOwnedChromiumProcess(pid) {
    if (!pid || typeof pid !== 'number' || pid <= 0) {
        return { isOwned: false, reason: 'Invalid or missing PID' };
    }
    if (!isProcessAlive(pid)) {
        return { isOwned: false, reason: `Process ${pid} is not alive` };
    }

    if (process.platform !== 'win32') {
        return { isOwned: true, reason: 'Non-Windows platform check bypass' };
    }

    const info = getProcessInfo(pid);
    if (!info || !info.Name) {
        return { isOwned: false, reason: `Could not query process info for PID ${pid}` };
    }

    const name = String(info.Name).toLowerCase();
    const isChromium = name === 'chrome.exe' || name === 'msedge.exe' || name === 'chromium.exe';
    if (!isChromium) {
        return {
            isOwned: false,
            reason: `Process ${pid} executable is "${info.Name}", not a Chromium browser`
        };
    }

    const cmdLine = String(info.CommandLine || '').toLowerCase();
    const sessionDirNormalized = path.resolve(sessionDataPath).toLowerCase();

    if (!cmdLine.includes(sessionDirNormalized)) {
        return {
            isOwned: false,
            reason: `Chromium process ${pid} command line does not reference session path "${sessionDirNormalized}"`
        };
    }

    return { isOwned: true, reason: 'Verified Chromium process owning WhatsApp session profile' };
}

/**
 * Terminate a browser process tree ONLY after ownership verification.
 * NEVER kills unrelated user Chrome/Edge processes.
 */
function terminateOwnedBrowser(pid, reason = 'Cleanup') {
    if (!pid || typeof pid !== 'number' || pid <= 0) {
        log('debug', `terminateOwnedBrowser skipped: No valid PID (${pid}).`, { component: 'Process' });
        return false;
    }

    const ownership = isOwnedChromiumProcess(pid);
    if (!ownership.isOwned) {
        log('warn', `Refusing to terminate PID ${pid}: ${ownership.reason}. Unrelated process will NOT be touched.`, { component: 'Process' });
        return false;
    }

    log('warn', `Terminating verified owned browser process tree (PID: ${pid}, Reason: ${reason})...`, { component: 'Process' });
    try {
        if (process.platform === 'win32') {
            execSync(`taskkill /pid ${pid} /T /F`, { stdio: 'ignore', timeout: 5000 });
        } else {
            process.kill(pid, 'SIGKILL');
        }
        log('info', `Verified browser process tree (PID: ${pid}) successfully terminated.`, { component: 'Process' });
        return true;
    } catch (err) {
        log('debug', `Termination note for PID ${pid}: ${err.message}`, { component: 'Process' });
        return false;
    }
}

/**
 * Fast TCP probe to check if a local port is currently listening.
 */
function isPortListening(testPort, timeoutMs = 500) {
    return new Promise((resolve) => {
        if (!testPort || isNaN(testPort)) return resolve(false);
        const socket = new net.Socket();
        let status = false;

        socket.setTimeout(timeoutMs);
        socket.once('connect', () => {
            status = true;
            socket.destroy();
            resolve(true);
        });
        socket.once('timeout', () => {
            socket.destroy();
            resolve(false);
        });
        socket.once('error', () => {
            socket.destroy();
            resolve(false);
        });

        socket.connect(testPort, '127.0.0.1');
    });
}

/**
 * On Windows, query netstat to find the specific PID listening on a TCP port.
 */
function getPidListeningOnPort(portNum) {
    if (process.platform !== 'win32' || !portNum) return null;
    try {
        const output = execSync('netstat -ano -p tcp', { stdio: ['pipe', 'pipe', 'ignore'], timeout: 3000 }).toString();
        const lines = output.split('\n');
        for (const line of lines) {
            if (line.includes(`:${portNum} `) && line.includes('LISTENING')) {
                const parts = line.trim().split(/\s+/);
                const pid = parseInt(parts[parts.length - 1], 10);
                if (!isNaN(pid) && pid > 0) return pid;
            }
        }
    } catch (_) {}
    return null;
}

// ── Stale Lock Files Management ───────────────────────────────────────────────
/**
 * Clean stale browser lock and port files left by crashed Chromium instances.
 * Explicit logic:
 *   - If DevTools port is active -> inspect listening PID.
 *   - If PID is an owned Chromium process -> terminate it safely.
 *   - If PID is NOT owned -> DO NOT kill, DO NOT delete locks, return safe:false.
 *   - Only remove files if port is dead and lock files are unlocked.
 */
async function cleanStaleBrowserLocks() {
    const sessionPath = path.join(sessionDataPath, 'session');
    if (!fs.existsSync(sessionPath)) return { safe: true };

    const devToolsPath = path.join(sessionPath, 'DevToolsActivePort');
    if (fs.existsSync(devToolsPath)) {
        try {
            const content = fs.readFileSync(devToolsPath, 'utf8').trim();
            const firstLine = content.split('\n')[0].trim();
            const portNum = Number(firstLine);

            if (portNum > 0) {
                const listening = await isPortListening(portNum, 600);
                if (listening) {
                    log('warn', `DevToolsActivePort ${portNum} is currently ACTIVE. An active process is listening.`, { component: 'Chromium' });
                    
                    const orphanPid = getPidListeningOnPort(portNum);
                    if (orphanPid) {
                        const ownership = isOwnedChromiumProcess(orphanPid);
                        if (ownership.isOwned) {
                            log('warn', `Identified verified orphaned Chromium PID ${orphanPid} on port ${portNum}. Terminating...`, { component: 'Chromium' });
                            terminateOwnedBrowser(orphanPid, `Orphaned DevTools port ${portNum}`);
                            await new Promise((r) => setTimeout(r, 1000));
                        } else {
                            log('warn', `Process ${orphanPid} on port ${portNum} is NOT an owned Chromium process (${ownership.reason}). Refusing to terminate.`, { component: 'Chromium' });
                            return { safe: false, reason: `Port ${portNum} is occupied by an unverified process (PID: ${orphanPid}).` };
                        }
                    } else {
                        log('warn', `Port ${portNum} is listening but PID could not be identified. Refusing to delete locks.`, { component: 'Chromium' });
                        return { safe: false, reason: `DevTools port ${portNum} is active on an unidentified process.` };
                    }
                }

                // Re-check port after potential kill
                const stillListening = await isPortListening(portNum, 400);
                if (!stillListening && fs.existsSync(devToolsPath)) {
                    log('info', `DevToolsActivePort ${portNum} is confirmed dead. Removing stale port file.`, { component: 'Chromium' });
                    fs.unlinkSync(devToolsPath);
                } else if (stillListening) {
                    return { safe: false, reason: `DevTools port ${portNum} remains active. Aborting lock cleanup.` };
                }
            }
        } catch (e) {
            log('warn', `Could not inspect DevToolsActivePort: ${e.message}`, { component: 'Chromium' });
        }
    }

    // Inspect SingletonLock and SingletonCookie
    const lockFiles = ['SingletonLock', 'SingletonCookie'];
    for (const fileName of lockFiles) {
        const filePath = path.join(sessionPath, fileName);
        if (fs.existsSync(filePath)) {
            try {
                // Non-destructive check: if open succeeds with r+, no process holds an exclusive lock
                const fd = fs.openSync(filePath, 'r+');
                fs.closeSync(fd);
                log('info', `Removing unlocked stale file: ${fileName}`, { component: 'Chromium' });
                fs.unlinkSync(filePath);
            } catch (err) {
                if (err.code === 'EBUSY' || err.code === 'EPERM') {
                    log('warn', `Cannot remove ${fileName}: handle is locked by an active process.`, { component: 'Chromium' });
                    return { safe: false, reason: `${fileName} is currently locked by a process.` };
                }
            }
        }
    }

    return { safe: true };
}

// ── Enhanced Logger with Rotation ─────────────────────────────────────────────
let clientStatus = 'STOPPED';
let clientGeneration = 0;
let restartCount = 0;

function log(level, message, meta = {}) {
    const timestamp = new Date().toISOString();
    const component = meta.component || 'WhatsApp';
    const currentGen = meta.generation !== undefined ? meta.generation : clientGeneration;
    const attemptStr = restartCount > 0 ? ` [attempt=${restartCount}]` : '';

    const logLine = `[${timestamp}] [${level.toUpperCase()}] [${component}] [generation=${currentGen}] [${clientStatus}]${attemptStr} ${message}`;

    if (level === 'error') {
        console.error(logLine);
    } else if (level === 'warn') {
        console.warn(logLine);
    } else {
        console.log(logLine);
    }

    if (logDir) {
        try {
            fs.mkdirSync(logDir, { recursive: true });
            const logFile = path.join(logDir, 'whatsapp.log');

            // Rotate log if size exceeds threshold
            if (fs.existsSync(logFile)) {
                const stats = fs.statSync(logFile);
                if (stats.size > MAX_LOG_SIZE_BYTES) {
                    const backupFile = path.join(logDir, 'whatsapp.log.1');
                    try {
                        if (fs.existsSync(backupFile)) fs.unlinkSync(backupFile);
                        fs.renameSync(logFile, backupFile);
                    } catch (_) {}
                }
            }
            fs.appendFileSync(logFile, logLine + '\n', 'utf8');
        } catch (_) {
            // Non-fatal logging error
        }
    }
}

// Ensure session directory exists
try {
    fs.mkdirSync(sessionDataPath, { recursive: true });
} catch (_) {}

// ── In-Memory Send Queue ──────────────────────────────────────────────────────
class SendQueue {
    constructor() {
        this.queue = [];
        this.isProcessing = false;
    }

    enqueue(taskFn) {
        return new Promise((resolve, reject) => {
            this.queue.push({
                taskFn,
                resolve,
                reject,
                enqueuedAt: Date.now()
            });
            this.process();
        });
    }

    async process() {
        if (this.isProcessing || this.queue.length === 0) return;
        this.isProcessing = true;

        while (this.queue.length > 0) {
            const item = this.queue.shift();

            // Check if client is still in READY state
            if (clientStatus !== 'READY') {
                const err = new Error(`WhatsApp client unavailable (Status: ${clientStatus}). Message rejected.`);
                err.statusCode = 503;
                item.reject(err);
                continue;
            }

            if (Date.now() - item.enqueuedAt > 30000) {
                const timeoutErr = new Error('Send operation timed out in queue');
                timeoutErr.statusCode = 504;
                item.reject(timeoutErr);
                continue;
            }

            try {
                const result = await item.taskFn();
                item.resolve(result);
            } catch (err) {
                item.reject(err);
            }

            // Brief pacing pause between consecutive sends
            await new Promise((r) => setTimeout(r, 250));
        }

        this.isProcessing = false;
    }

    clear(reason = 'Send queue cleared') {
        const count = this.queue.length;
        if (count > 0) {
            log('warn', `Clearing ${count} pending queued sends with HTTP 503. Reason: ${reason}`, { component: 'Queue' });
        }
        while (this.queue.length > 0) {
            const item = this.queue.shift();
            const err = new Error(`WhatsApp client recovering: ${reason}`);
            err.statusCode = 503;
            item.reject(err);
        }
    }
}

const sendQueue = new SendQueue();

// ── Phone Number Normalization ────────────────────────────────────────────────
function normalizePhoneNumber(rawPhone) {
    if (!rawPhone || (typeof rawPhone !== 'string' && typeof rawPhone !== 'number')) {
        return { valid: false, error: 'Phone number must be a non-empty string or number.' };
    }
    const original = String(rawPhone).trim();
    if (!original) {
        return { valid: false, error: 'Phone number cannot be empty.' };
    }

    let cleaned = original.replace(/[\s\-\(\)\.]/g, '');

    if (cleaned.startsWith('+')) {
        cleaned = cleaned.substring(1);
    } else if (cleaned.startsWith('00')) {
        cleaned = cleaned.substring(2);
    }

    if (!/^\d+$/.test(cleaned)) {
        return { valid: false, error: `Phone number "${original}" contains invalid characters.` };
    }

    // Moroccan formats:
    // 06XXXXXXXX, 07XXXXXXXX, 05XXXXXXXX (10 digits) -> 2126..., 2127..., 2125...
    if (/^0[567]\d{8}$/.test(cleaned)) {
        cleaned = '212' + cleaned.substring(1);
    }
    // 6XXXXXXXX, 7XXXXXXXX, 5XXXXXXXX (9 digits) -> 2126..., 2127..., 2125...
    else if (/^[567]\d{8}$/.test(cleaned)) {
        cleaned = '212' + cleaned;
    }
    // Already has Moroccan country code: 212 followed by 5/6/7 and 8 digits (12 digits total)
    else if (/^212[567]\d{8}$/.test(cleaned)) {
        // Valid Moroccan number
    }
    // International numbers (E.164: 8 to 15 digits total)
    else if (/^[1-9]\d{7,14}$/.test(cleaned)) {
        // Valid international format
    } else {
        return {
            valid: false,
            error: `Invalid phone number format "${original}". Must be a valid Moroccan number (e.g. 06XXXXXXXX, 07XXXXXXXX, +2126XXXXXXXX) or international number.`
        };
    }

    const chatId = `${cleaned}@c.us`;
    return { valid: true, cleanedPhone: cleaned, chatId };
}

// ── State Machine & Client Lifecycle Management ──────────────────────────────
// States: STOPPED, STARTING, QR_REQUIRED, AUTHENTICATED, READY, DISCONNECTED, RESTART_WAIT, ERROR, SHUTTING_DOWN
let client = null;
let clientInfo = null;
let qrCodeData = null;
let activeBrowserPid = null;

let lastReadyAt = null;
let lastError = null;
let restartTimer = null;
let authToReadyTimer = null;
let isExecutingAction = false;
const actionQueue = [];
const invalidatedGenerations = new Set();

function setStatus(newStatus) {
    const prevStatus = clientStatus;
    clientStatus = newStatus;
    log('info', `State changed: ${prevStatus} -> ${newStatus}`);

    // If leaving READY, reject any pending sends with HTTP 503 immediately
    if (prevStatus === 'READY' && newStatus !== 'READY') {
        sendQueue.clear(`Client transitioned to ${newStatus}`);
    }
}

/**
 * Serialized async action runner ensuring create, destroy, and restart
 * operations never race against each other.
 */
function enqueueAction(actionFn, isInitAction = false) {
    return new Promise((resolve, reject) => {
        if (clientStatus === 'SHUTTING_DOWN') {
            return reject(new Error('Service is shutting down'));
        }

        // Deduplication: if an initialization action is already waiting, drop duplicates
        if (isInitAction) {
            const hasPendingInit = actionQueue.some((item) => item.isInitAction);
            if (hasPendingInit) {
                log('debug', 'An initialization action is already queued. Dropping duplicate request.');
                return resolve({ deduplicated: true });
            }
        }

        actionQueue.push({ actionFn, resolve, reject, isInitAction });
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
 * Destroys existing WhatsApp client and browser instance safely with timeouts.
 * Always attempts to capture and terminate only the verified owned Chromium PID.
 */
async function destroyCurrentClient(reason = 'Teardown', clientOverride = null, pidOverride = null) {
    if (restartTimer) {
        clearTimeout(restartTimer);
        restartTimer = null;
    }
    if (authToReadyTimer) {
        clearTimeout(authToReadyTimer);
        authToReadyTimer = null;
    }

    const clientToDestroy = clientOverride || client;
    const pidToDestroy = pidOverride || getBrowserPid(clientToDestroy) || activeBrowserPid;

    if (client === clientToDestroy) {
        client = null;
    }
    if (activeBrowserPid === pidToDestroy) {
        activeBrowserPid = null;
    }

    if (!clientToDestroy && !pidToDestroy) {
        return;
    }

    log('info', `Initiating safe client and browser destruction (Reason: ${reason}, Target PID: ${pidToDestroy || 'unknown'})...`);

    if (clientToDestroy) {
        let timeoutHandle = null;

        const destroyPromise = Promise.resolve().then(async () => {
            try {
                clientToDestroy.removeAllListeners();
            } catch (_) {}
            if (typeof clientToDestroy.destroy === 'function') {
                await clientToDestroy.destroy();
            }
        });

        const timeoutPromise = new Promise((_, reject) => {
            timeoutHandle = setTimeout(() => {
                reject(new Error(`client.destroy() timed out after ${DESTROY_TIMEOUT_MS}ms`));
            }, DESTROY_TIMEOUT_MS);
        });

        try {
            await Promise.race([destroyPromise, timeoutPromise]);
            log('info', 'WhatsApp client destroyed cleanly.');
        } catch (err) {
            log('warn', `Graceful client destroy failed: ${err.message}. Enforcing process-level termination.`);
        } finally {
            if (timeoutHandle) clearTimeout(timeoutHandle);
        }
    }

    // Terminate verified owned browser process tree if still alive
    if (pidToDestroy) {
        terminateOwnedBrowser(pidToDestroy, reason);
    }

    // Wait for Windows file handles and directory locks to be released
    await new Promise((r) => setTimeout(r, 1200));
    await cleanStaleBrowserLocks();
}

/**
 * Initialize a new WhatsApp Client with timeout and deterministic lifecycle guards.
 */
async function initializeNewClient(triggerReason = 'Standard') {
    if (clientStatus === 'SHUTTING_DOWN') return;

    // Increment client generation so all previous callbacks become immediate no-ops
    clientGeneration += 1;
    const thisGeneration = clientGeneration;

    log('info', `Starting client initialization (Reason: ${triggerReason})...`, { generation: thisGeneration });

    // Ensure previous instance is completely destroyed before proceeding
    await destroyCurrentClient(`Re-init [${triggerReason}]`);

    // Verify stale locks; if safe cleanup failed, schedule backoff and wait
    const lockCheck = await cleanStaleBrowserLocks();
    if (!lockCheck.safe) {
        log('warn', `Cannot safely initialize: ${lockCheck.reason}. Scheduling backoff retry.`, { generation: thisGeneration });
        setStatus('ERROR');
        scheduleRestart(5000, 'Lock check failed');
        return;
    }

    setStatus('STARTING');
    qrCodeData = null;
    clientInfo = null;

    // Windows-optimized Puppeteer flags (no Linux/Docker-only flags)
    const puppeteerArgs = [
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-gpu',
        '--disable-accelerated-2d-canvas',
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-breakpad',
        '--disable-component-update',
        '--disable-extensions',
        '--disable-ipc-flooding-protection',
        '--disable-renderer-backgrounding',
        '--disable-dev-shm-usage',
        '--mute-audio',
        '--js-flags=--max-old-space-size=512'
    ];

    if (process.env.WA_DISABLE_SANDBOX === 'true') {
        puppeteerArgs.push('--no-sandbox', '--disable-setuid-sandbox');
    }

    const puppeteerConfig = {
        headless: true,
        args: puppeteerArgs
    };

    if (customChromePath) {
        log('info', `Using custom Chromium binary at: ${customChromePath}`, { generation: thisGeneration });
        puppeteerConfig.executablePath = customChromePath;
    } else {
        log('info', 'Using Puppeteer configured Chromium browser.', { generation: thisGeneration });
    }

    const newClient = new Client({
        authStrategy: new LocalAuth({
            dataPath: sessionDataPath
        }),
        puppeteer: puppeteerConfig
    });

    // ── Generation-guarded Event Listeners ────────────────────────────────────
    newClient.on('qr', async (qr) => {
        if (thisGeneration !== clientGeneration || invalidatedGenerations.has(thisGeneration)) {
            log('debug', 'Ignored qr event from stale client generation.', { generation: thisGeneration });
            return;
        }
        setStatus('QR_REQUIRED');
        log('info', 'QR Code generated. Scan via School ERP WhatsApp settings.', { generation: thisGeneration });
        try {
            qrCodeData = await qrcode.toDataURL(qr);
        } catch (err) {
            log('error', `Failed to generate QR data URL: ${err.message}`, { generation: thisGeneration });
        }
    });

    newClient.on('authenticated', () => {
        if (thisGeneration !== clientGeneration || invalidatedGenerations.has(thisGeneration)) {
            log('debug', 'Ignored authenticated event from stale client generation.', { generation: thisGeneration });
            return;
        }
        setStatus('AUTHENTICATED');
        qrCodeData = null;
        log('info', 'Authenticated successfully with WhatsApp. Loading chats...', { generation: thisGeneration });

        // Guard against WhatsApp Web hanging between authentication and ready
        if (authToReadyTimer) clearTimeout(authToReadyTimer);
        authToReadyTimer = setTimeout(() => {
            if (thisGeneration !== clientGeneration || invalidatedGenerations.has(thisGeneration)) return;
            if (clientStatus === 'AUTHENTICATED') {
                log('error', `Ready event did not arrive within ${AUTH_TO_READY_TIMEOUT_MS}ms after authentication. Initiating complete cleanup and recovery.`, { generation: thisGeneration });
                invalidatedGenerations.add(thisGeneration);
                lastError = 'Ready event timeout after authentication';
                setStatus('ERROR');

                // Perform complete teardown and restart cycle
                enqueueAction(async () => {
                    await destroyCurrentClient(`Auth to ready timeout (gen ${thisGeneration})`, newClient, activeBrowserPid);
                    scheduleRestart(5000, 'Auth-to-ready timeout');
                }).catch((err) => {
                    log('error', `Auth-to-ready recovery action failed: ${err.message}`);
                });
            }
        }, AUTH_TO_READY_TIMEOUT_MS);
    });

    newClient.on('auth_failure', (msg) => {
        if (thisGeneration !== clientGeneration || invalidatedGenerations.has(thisGeneration)) {
            log('debug', 'Ignored auth_failure event from stale client generation.', { generation: thisGeneration });
            return;
        }
        log('error', `WhatsApp authentication failed: ${msg}. Session may need re-pairing.`, { generation: thisGeneration });
        invalidatedGenerations.add(thisGeneration);

        if (authToReadyTimer) {
            clearTimeout(authToReadyTimer);
            authToReadyTimer = null;
        }
        setStatus('ERROR');
        lastError = `Authentication failed: ${msg}`;

        try {
            newClient.removeAllListeners();
        } catch (_) {}

        // Controlled teardown without deleting session directory
        enqueueAction(async () => {
            const authFailPid = getBrowserPid(newClient) || activeBrowserPid;
            await destroyCurrentClient(`Auth failure: ${msg}`, newClient, authFailPid);
            scheduleRestart(5000, 'Authentication failure');
        }).catch((err) => {
            log('error', `Auth failure cleanup action failed: ${err.message}`);
        });
    });

    newClient.on('ready', () => {
        if (thisGeneration !== clientGeneration || invalidatedGenerations.has(thisGeneration)) {
            log('debug', 'Ignored ready event from stale client generation.', { generation: thisGeneration });
            return;
        }
        if (authToReadyTimer) {
            clearTimeout(authToReadyTimer);
            authToReadyTimer = null;
        }
        setStatus('READY');
        clientInfo = newClient.info;
        lastReadyAt = new Date().toISOString();
        restartCount = 0; // Reset restart backoff counter upon reaching READY
        lastError = null;
        log('info', 'WhatsApp Client is fully READY for messaging.', { generation: thisGeneration });
    });

    newClient.on('disconnected', (reason) => {
        if (thisGeneration !== clientGeneration || invalidatedGenerations.has(thisGeneration)) {
            log('debug', 'Ignored disconnected event from stale client generation.', { generation: thisGeneration });
            return;
        }
        if (clientStatus === 'SHUTTING_DOWN') return;

        invalidatedGenerations.add(thisGeneration);
        if (authToReadyTimer) {
            clearTimeout(authToReadyTimer);
            authToReadyTimer = null;
        }
        setStatus('DISCONNECTED');
        lastError = `Disconnected: ${reason}`;
        log('warn', `WhatsApp client disconnected. Reason: ${reason}`, { generation: thisGeneration });
        scheduleRestart(3000, `Disconnected: ${reason}`);
    });

    newClient.on('change_state', (state) => {
        if (thisGeneration !== clientGeneration || invalidatedGenerations.has(thisGeneration)) return;
        log('info', `WhatsApp connection state changed to: ${state}`, { generation: thisGeneration });
    });

    client = newClient;

    // ── Active PID Detection Loop During Initialization ───────────────────────
    // Polling interval captures Chromium PID as soon as Puppeteer launches it,
    // before WhatsApp Web page navigation finishes or hangs.
    const pidPollInterval = setInterval(() => {
        if (thisGeneration !== clientGeneration || invalidatedGenerations.has(thisGeneration)) {
            clearInterval(pidPollInterval);
            return;
        }
        const detectedPid = getBrowserPid(newClient);
        if (detectedPid && detectedPid !== activeBrowserPid) {
            activeBrowserPid = detectedPid;
            log('info', `Chromium PID identified: ${activeBrowserPid}`, { generation: thisGeneration });

            const proc = getBrowserProcess(newClient);
            if (proc) {
                proc.once('exit', (code, signal) => {
                    if (thisGeneration !== clientGeneration || invalidatedGenerations.has(thisGeneration)) return;
                    if (clientStatus === 'SHUTTING_DOWN') return;

                    log('error', `Chromium process (PID: ${detectedPid}) exited unexpectedly (code: ${code}, signal: ${signal}).`, { generation: thisGeneration });
                    invalidatedGenerations.add(thisGeneration);
                    lastError = `Chromium exited unexpectedly (code: ${code})`;
                    setStatus('ERROR');
                    scheduleRestart(3000, 'Chromium process exit');
                });
            }
            clearInterval(pidPollInterval);
        }
    }, 250);

    // ── Initialization with Timeout Guard ─────────────────────────────────────
    let initTimeoutHandle = null;
    const initTimeoutPromise = new Promise((_, reject) => {
        initTimeoutHandle = setTimeout(() => {
            reject(new Error(`Initialization timeout after ${INIT_TIMEOUT_MS}ms`));
        }, INIT_TIMEOUT_MS);
    });

    try {
        log('info', `Initializing client with ${INIT_TIMEOUT_MS}ms timeout guard...`, { generation: thisGeneration });
        await Promise.race([
            newClient.initialize(),
            initTimeoutPromise
        ]);

        // Capture browser PID upon successful resolution if not already caught by poll
        const finalPid = getBrowserPid(newClient);
        if (finalPid && !activeBrowserPid) {
            activeBrowserPid = finalPid;
            log('info', `Chromium PID identified: ${activeBrowserPid}`, { generation: thisGeneration });
        }
        log('info', 'WhatsApp Web loading...', { generation: thisGeneration });
    } catch (err) {
        clearInterval(pidPollInterval);

        // Invalidate generation immediately: Promise.race does NOT cancel initialize(),
        // so we guarantee late events or resolutions from this generation are dropped.
        invalidatedGenerations.add(thisGeneration);

        // Attempt to capture browser PID immediately on failure path
        const failedPid = getBrowserPid(newClient) || activeBrowserPid;
        log('error', `Client initialization failed: ${err.message}. Captured PID for cleanup: ${failedPid || 'unknown'}`, { generation: thisGeneration });
        lastError = err.message;
        setStatus('ERROR');

        // Immediately detach all listeners so late events from this failed client are discarded
        try {
            newClient.removeAllListeners();
        } catch (_) {}

        // Enforce safe destruction and terminate only verified owned browser process
        await destroyCurrentClient(`Init failure: ${err.message}`, newClient, failedPid);
        scheduleRestart(5000, 'Initialization failure');
    } finally {
        clearInterval(pidPollInterval);
        if (initTimeoutHandle) clearTimeout(initTimeoutHandle);
    }
}

/**
 * Schedules a restart with exponential backoff and authoritative deduplication.
 */
function scheduleRestart(baseDelayMs = 3000, triggerReason = 'Generic') {
    if (clientStatus === 'SHUTTING_DOWN') return;

    // Coalesce duplicate restart requests: only one timer may be scheduled at a time
    if (restartTimer) {
        log('debug', `Restart already scheduled, skipping duplicate trigger (${triggerReason}).`);
        return;
    }

    // Check if an initialization action is already waiting in queue
    const hasPendingInit = actionQueue.some((item) => item.isInitAction);
    if (hasPendingInit) {
        log('debug', `Initialization action already pending in queue, skipping trigger (${triggerReason}).`);
        return;
    }

    restartCount += 1;
    // Calculate backoff: min(baseDelay * 1.5^(count - 1), RESTART_MAX_DELAY_MS)
    const backoff = Math.min(
        Math.round(baseDelayMs * Math.pow(1.5, Math.max(0, restartCount - 1))),
        RESTART_MAX_DELAY_MS
    );

    setStatus('RESTART_WAIT');
    log('info', `Scheduling client restart in ${backoff}ms (attempt #${restartCount}, max ${RESTART_MAX_DELAY_MS}ms, trigger: ${triggerReason})...`);

    restartTimer = setTimeout(() => {
        restartTimer = null;
        enqueueAction(() => initializeNewClient(`restart_attempt_${restartCount}`), true).catch((err) => {
            log('error', `Scheduled restart failed to enqueue: ${err.message}`);
        });
    }, backoff);
}

// ── Graceful Shutdown ────────────────────────────────────────────────────────
let isShuttingDown = false;

async function handleShutdown(signal) {
    if (isShuttingDown) return;
    isShuttingDown = true;
    setStatus('SHUTTING_DOWN');
    log('info', `Received ${signal || 'shutdown'} signal. Initiating graceful shutdown...`);

    // Hard emergency exit watchdog in case shutdown stalls
    const forceExitTimer = setTimeout(() => {
        log('warn', 'Shutdown watchdog expired (15s). Forcing process exit.');
        process.exit(1);
    }, 15000);
    forceExitTimer.unref();

    sendQueue.clear('Service shutting down');

    if (restartTimer) {
        clearTimeout(restartTimer);
        restartTimer = null;
    }
    if (authToReadyTimer) {
        clearTimeout(authToReadyTimer);
        authToReadyTimer = null;
    }

    try {
        await destroyCurrentClient('Shutdown');
    } catch (err) {
        log('error', `Error during client teardown: ${err.message}`);
    }

    log('info', 'WhatsApp service shutdown complete. Exiting cleanly.');
    process.exit(0);
}

process.on('SIGTERM', () => handleShutdown('SIGTERM'));
process.on('SIGINT', () => handleShutdown('SIGINT'));

// Windows console readline support for interactive environments
if (process.platform === 'win32') {
    const rl = readline.createInterface({
        input: process.stdin,
        output: process.stdout
    });
    rl.on('SIGINT', () => {
        process.emit('SIGINT');
    });
}

// ── Uncaught Exception & Rejection Handlers ──────────────────────────────────
process.on('uncaughtException', (err) => {
    log('error', `FATAL UNCAUGHT EXCEPTION: ${err.message}`, { stack: err.stack, component: 'System' });

    // In production on Windows, uncaughtException indicates the Node process runtime is in
    // an undefined/corrupted state. Attempting to continue in-process execution risks memory leaks
    // and deadlocked sockets. The safe production practice is to perform immediate emergency browser
    // termination and exit with code 1, allowing the Windows Service Manager (NSSM/PM2/Task Scheduler)
    // to cleanly restart the entire service.
    try {
        if (activeBrowserPid) {
            terminateOwnedBrowser(activeBrowserPid, 'Emergency uncaughtException exit');
        }
    } catch (_) {}

    setTimeout(() => {
        process.exit(1);
    }, 1000).unref();
});

process.on('unhandledRejection', (reason) => {
    const msg = reason instanceof Error ? reason.message : String(reason);
    const stack = reason instanceof Error ? reason.stack : null;
    log('error', `UNHANDLED REJECTION: ${msg}`, { stack, component: 'System' });
});

// ── Express Application & Middleware ──────────────────────────────────────────
const app = express();
app.use(express.json({ limit: '100mb' }));

function requireApiKey(req, res, next) {
    if (!API_KEY) return next();
    const key = req.headers['x-api-key'];
    if (key === API_KEY) return next();
    return res.status(401).json({ success: false, error: 'Unauthorized' });
}

// ── API Endpoints ────────────────────────────────────────────────────────────

// GET /status — Structured status response for School ERP
app.get('/status', (req, res) => {
    res.json({
        service: 'running',
        status: clientStatus,
        qr: qrCodeData,
        info: clientInfo,
        version: SERVICE_VERSION,
        uptime: process.uptime(),
        restartCount: restartCount,
        clientExists: Boolean(client),
        isInitializing: clientStatus === 'STARTING' || isExecutingAction,
        lastReadyAt: lastReadyAt,
        lastError: lastError,
        clientGeneration: clientGeneration,
        retryInfo: {
            isWaiting: clientStatus === 'RESTART_WAIT',
            restartCount: restartCount,
            maxBackoffMs: RESTART_MAX_DELAY_MS
        }
    });
});

// POST /send — Send message or media attachment via the in-memory queue
app.post('/send', requireApiKey, async (req, res) => {
    const { phone, message = '', attachments } = req.body;
    const hasAttachments = Array.isArray(attachments) && attachments.length > 0;

    if (!phone || (!message && !hasAttachments)) {
        return res.status(400).json({
            success: false,
            error: 'Phone and message or attachments are required'
        });
    }

    // Verify client state
    if (clientStatus !== 'READY' || !client) {
        return res.status(503).json({
            success: false,
            error: `WhatsApp client is not ready (Current status: ${clientStatus}). Please retry once the service is READY.`
        });
    }

    // Normalize and validate recipient phone number
    const phoneResult = normalizePhoneNumber(phone);
    if (!phoneResult.valid) {
        return res.status(400).json({
            success: false,
            error: phoneResult.error
        });
    }
    const { chatId } = phoneResult;

    // Validate attachments if present
    if (hasAttachments) {
        for (let i = 0; i < attachments.length; i++) {
            const att = attachments[i];
            if (!att.name || !att.data) {
                return res.status(400).json({
                    success: false,
                    error: `Attachment #${i + 1} is missing required 'name' or 'data' fields.`
                });
            }
            // Base64 size check: 1 base64 char ~= 0.75 byte
            const approximateBytes = Math.ceil(att.data.length * 0.75);
            if (approximateBytes > MAX_ATTACHMENT_SIZE_MB * 1024 * 1024) {
                return res.status(400).json({
                    success: false,
                    error: `Attachment "${att.name}" exceeds maximum allowed size of ${MAX_ATTACHMENT_SIZE_MB}MB.`
                });
            }
        }
    }

    // Submit to in-memory send queue
    try {
        const result = await sendQueue.enqueue(async () => {
            if (clientStatus !== 'READY' || !client) {
                const err = new Error(`Client disconnected before message could be sent (Status: ${clientStatus})`);
                err.statusCode = 503;
                throw err;
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
            } else {
                lastResponse = await client.sendMessage(chatId, message);
                log('info', `Message successfully sent to ${chatId}.`);
            }

            return { success: true, messageId: lastResponse?.id?.id || null };
        });

        return res.json(result);
    } catch (err) {
        const status = err.statusCode || 500;
        log('error', `Send error to ${phone}: ${err.message}`);
        return res.status(status).json({ success: false, error: err.message });
    }
});

// POST /logout — Clear authentication and prepare fresh client for re-pairing
app.post('/logout', requireApiKey, async (req, res) => {
    try {
        log('info', 'Logging out from WhatsApp session via API...');
        res.json({ success: true, message: 'Logged out successfully' });

        enqueueAction(async () => {
            if (client) {
                try {
                    await client.logout();
                } catch (err) {
                    log('warn', `Logout error on client: ${err.message}`);
                }
            }
            await destroyCurrentClient('API Logout');

            // Explicitly clean session directory on explicit logout request
            const sessionDir = path.join(sessionDataPath, 'session');
            try {
                if (fs.existsSync(sessionDir)) {
                    fs.rmSync(sessionDir, { recursive: true, force: true });
                    log('info', 'Session files removed after explicit logout.');
                }
            } catch (e) {
                log('warn', `Failed to remove session folder: ${e.message}`);
            }

            await initializeNewClient('Post-logout fresh client');
        }, true).catch((err) => {
            log('error', `Re-init after logout failed: ${err.message}`);
        });
    } catch (error) {
        log('error', `Logout endpoint error: ${error.message}`);
        res.status(500).json({ success: false, error: error.message });
    }
});

// POST /restart — Manually restart the WhatsApp client without restarting Node
app.post('/restart', requireApiKey, async (req, res) => {
    log('info', 'Manual restart requested via API...');
    res.json({ success: true, message: 'Restart initiated' });

    enqueueAction(() => initializeNewClient('API Manual Restart'), true).catch((err) => {
        log('error', `Manual restart failed: ${err.message}`);
    });
});

// ── Server Start & Windows Startup Delay ──────────────────────────────────────
const server = app.listen(port, host, () => {
    log('info', `WhatsApp automation service listening at http://${host}:${port}`);
    if (API_KEY) {
        log('info', 'API key authentication is ENABLED.');
    } else {
        log('info', 'API key authentication is DISABLED (set WA_API_KEY env var to enable).');
    }

    // Windows startup stabilization delay
    if (STARTUP_DELAY_MS > 0) {
        log('info', `Waiting ${STARTUP_DELAY_MS}ms for Windows networking/services stabilization before initial launch...`);
        setTimeout(() => {
            enqueueAction(() => initializeNewClient('Initial Startup'), true).catch((err) => {
                log('error', `Initial client startup failed: ${err.message}`);
            });
        }, STARTUP_DELAY_MS);
    } else {
        enqueueAction(() => initializeNewClient('Immediate Startup'), true).catch((err) => {
            log('error', `Initial client startup failed: ${err.message}`);
        });
    }
});