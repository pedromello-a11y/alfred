require('dotenv').config();

const fs = require('fs');
const path = require('path');
const express = require('express');
const qrcode = require('qrcode');
const qrcodeTerminal = require('qrcode-terminal');
const { Client, RemoteAuth } = require('whatsapp-web.js');

const PostgresSessionStore = require('./session_store');
const state = require('./state');

fs.mkdirSync(path.join(__dirname, '.wwebjs_auth'), { recursive: true });

const PORT = Number(process.env.PORT || 3001);
const ALFRED_API_URL = (process.env.ALFRED_API_URL || '').replace(/\/$/, '');
const WA_BRIDGE_SHARED_SECRET = (process.env.WA_BRIDGE_SHARED_SECRET || '').trim();
const ALFRED_GROUP_NAME = (process.env.ALFRED_GROUP_NAME || 'Alfred').trim().toLowerCase();
const ALFRED_CHAT_ID = (process.env.ALFRED_CHAT_ID || '').trim();
const ALFRED_OUTBOUND_CHAT_ID = (process.env.ALFRED_OUTBOUND_CHAT_ID || '').trim();
const WA_SESSION_ID = (process.env.WA_SESSION_ID || 'alfred').trim();

let botResponding = false;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 3;

function getHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  if (WA_BRIDGE_SHARED_SECRET) {
    headers['X-Bridge-Secret'] = WA_BRIDGE_SHARED_SECRET;
  }
  return headers;
}

function normalizeChatId(value) {
  const raw = String(value || '').trim();
  if (!raw) throw new Error('chatId ausente');
  if (raw.endsWith('@g.us') || raw.endsWith('@c.us')) return raw;
  if (raw.endsWith('@s.whatsapp.net')) return raw.replace('@s.whatsapp.net', '@c.us');
  if (/^\d+$/.test(raw)) return `${raw}@c.us`;
  return raw;
}

async function resolveGroupByName(groupName) {
  const chats = await state.client.getChats();
  const match = chats.find(
    (chat) => chat.isGroup && (chat.name || '').trim().toLowerCase() === groupName.toLowerCase(),
  );
  return match ? match.id._serialized : null;
}

async function resolveOutboundChatId(target) {
  const raw = String(target || '').trim();

  if (raw.endsWith('@g.us') || raw.endsWith('@c.us') || raw.endsWith('@s.whatsapp.net')) {
    return normalizeChatId(raw);
  }

  if (/^\d+$/.test(raw)) {
    if (ALFRED_OUTBOUND_CHAT_ID) {
      return normalizeChatId(ALFRED_OUTBOUND_CHAT_ID);
    }
    if (ALFRED_CHAT_ID) {
      return normalizeChatId(ALFRED_CHAT_ID);
    }
    if (ALFRED_GROUP_NAME) {
      const resolved = await resolveGroupByName(ALFRED_GROUP_NAME);
      if (resolved) return resolved;
    }
    return normalizeChatId(raw);
  }

  return normalizeChatId(raw);
}

function isAllowedChat(chat) {
  if (ALFRED_CHAT_ID) {
    return chat.id && chat.id._serialized === ALFRED_CHAT_ID;
  }
  return chat.isGroup && (chat.name || '').trim().toLowerCase() === ALFRED_GROUP_NAME;
}

async function postInboundToAlfred(payload) {
  if (!ALFRED_API_URL) {
    throw new Error('ALFRED_API_URL não configurada');
  }

  const response = await fetch(`${ALFRED_API_URL}/internal/whatsapp/inbound`, {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify(payload),
  });

  const text = await response.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }

  if (!response.ok) {
    throw new Error(`Alfred API ${response.status}: ${text.slice(0, 300)}`);
  }

  return data;
}

const store = new PostgresSessionStore();
const authStrategy = new RemoteAuth({
  clientId: WA_SESSION_ID,
  store,
  backupSyncIntervalMs: 300000,
});

const originalStoreRemoteSession = authStrategy.storeRemoteSession.bind(authStrategy);
authStrategy.storeRemoteSession = async function patchedStoreRemoteSession(options) {
  try {
    await originalStoreRemoteSession(options);
  } catch (err) {
    if (err && err.code === 'ENOENT') {
      console.log('[session] ZIP ausente ao limpar, ignorando');
      return;
    }
    throw err;
  }
};

const puppeteerOptions = {
  headless: true,
  args: [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-extensions',
    '--single-process',
    '--no-zygote',
    '--disable-background-networking',
    '--disable-default-apps',
    '--mute-audio',
  ],
};

if (process.env.PUPPETEER_EXECUTABLE_PATH) {
  puppeteerOptions.executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
}

const client = new Client({
  authStrategy,
  puppeteer: puppeteerOptions,
});
state.client = client;

client.on('qr', async (qrRaw) => {
  state.qrRaw = qrRaw;
  state.botStatus = 'qr';
  try {
    state.qrCode = await qrcode.toDataURL(qrRaw);
  } catch {
    state.qrCode = null;
  }
  console.log('[wa-gateway] QR gerado. Escaneie em /qr ou no terminal abaixo.');
  qrcodeTerminal.generate(qrRaw, { small: true });
});

client.on('authenticated', () => {
  console.log('[wa-gateway] Autenticado com sucesso.');
});

client.on('auth_failure', (msg) => {
  state.botStatus = 'offline';
  console.error('[wa-gateway] Falha de autenticação:', msg);
});

client.on('ready', () => {
  reconnectAttempts = 0;
  state.qrCode = null;
  state.qrRaw = null;
  state.botStatus = 'ready';
  console.log('[wa-gateway] ✅ WhatsApp conectado e pronto.');
});

client.on('disconnected', (reason) => {
  state.botStatus = 'disconnected';
  console.error('[wa-gateway] Desconectado:', reason);

  if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
    console.error('[wa-gateway] Máximo de tentativas atingido. Aguarde novo QR.');
    state.botStatus = 'offline';
    reconnectAttempts = 0;
    return;
  }

  reconnectAttempts += 1;
  const delayMs = reconnectAttempts * 10000;
  console.log(`[wa-gateway] Tentativa ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS} em ${delayMs / 1000}s...`);
  setTimeout(() => {
    client.initialize().catch((err) => {
      console.error('[wa-gateway] Erro ao reinicializar:', err.message || err);
    });
  }, delayMs);
});

client.on('message_create', async (msg) => {
  try {
    if (msg.fromMe && botResponding) return;
    if (msg.from === 'status@broadcast') return;
    if (msg.type !== 'chat') return;

    const chat = await msg.getChat();
    if (!isAllowedChat(chat)) return;

    const text = (msg.body || '').trim();
    if (!text) return;

    state.lastInboundChatId = chat.id._serialized;

    const payload = {
      text,
      chat_id: chat.id._serialized,
      message_id: msg.id && msg.id._serialized ? msg.id._serialized : null,
      sender_id: String(msg.author || msg.from || '').replace(/@.*/, ''),
      sender_name: msg._data?.notifyName || chat.name || 'WhatsApp',
      source: 'whatsapp-web.js',
      from_me: Boolean(msg.fromMe),
      is_group: Boolean(chat.isGroup),
    };

    const data = await postInboundToAlfred(payload);
    if (!data.reply) return;

    botResponding = true;
    try {
      await chat.sendMessage(data.reply);
    } finally {
      botResponding = false;
    }
  } catch (err) {
    botResponding = false;
    console.error('[wa-gateway] Erro ao processar inbound:', err.message || err);
  }
});

const app = express();
app.use(express.json({ limit: '2mb' }));

app.get('/health', async (_req, res) => {
  res.json({
    status: 'ok',
    botStatus: state.botStatus,
    hasQr: Boolean(state.qrCode),
    hasApiUrl: Boolean(ALFRED_API_URL),
    lastInboundChatId: state.lastInboundChatId,
  });
});

app.get('/status', async (_req, res) => {
  res.json({
    botStatus: state.botStatus,
    hasQr: Boolean(state.qrCode),
    lastInboundChatId: state.lastInboundChatId,
    allowedChatId: ALFRED_CHAT_ID || null,
    allowedGroupName: ALFRED_CHAT_ID ? null : ALFRED_GROUP_NAME,
    outboundChatId: ALFRED_OUTBOUND_CHAT_ID || null,
  });
});

app.get('/qr', async (_req, res) => {
  if (!state.qrCode) {
    res.send(`
      <html>
        <body style="font-family: Arial, sans-serif; padding: 24px;">
          <h1>Alfred WhatsApp Gateway</h1>
          <p>Status atual: <strong>${state.botStatus}</strong></p>
          <p>Sem QR disponível agora. Se já autenticou, isso é esperado.</p>
        </body>
      </html>
    `);
    return;
  }

  res.send(`
    <html>
      <body style="font-family: Arial, sans-serif; padding: 24px;">
        <h1>Escaneie o QR do Alfred</h1>
        <p>Status atual: <strong>${state.botStatus}</strong></p>
        <img src="${state.qrCode}" alt="QR Code" style="max-width: 420px; width: 100%;" />
      </body>
    </html>
  `);
});

app.post('/send', async (req, res) => {
  try {
    if (state.botStatus !== 'ready') {
      res.status(503).json({ status: 'failed', error: 'gateway_not_ready' });
      return;
    }

    const providedChatId = req.body?.chatId;
    const text = String(req.body?.text || '').trim();
    const requestSecret = String(req.headers['x-bridge-secret'] || '').trim();

    if (WA_BRIDGE_SHARED_SECRET && requestSecret !== WA_BRIDGE_SHARED_SECRET) {
      res.status(401).json({ status: 'failed', error: 'invalid_bridge_secret' });
      return;
    }

    if (!text) {
      res.status(400).json({ status: 'failed', error: 'empty_text' });
      return;
    }

    const resolvedChatId = await resolveOutboundChatId(providedChatId);
    await client.sendMessage(resolvedChatId, text);
    res.json({ status: 'ok', chatId: resolvedChatId });
  } catch (err) {
    console.error('[wa-gateway] Erro no /send:', err.message || err);
    res.status(500).json({ status: 'failed', error: err.message || String(err) });
  }
});

app.listen(PORT, () => {
  console.log(`[wa-gateway] HTTP escutando na porta ${PORT}`);
});

console.log('[wa-gateway] Iniciando WhatsApp gateway do Alfred...');
client.initialize().catch((err) => {
  state.botStatus = 'offline';
  console.error('[wa-gateway] Erro ao inicializar client:', err.message || err);
});
