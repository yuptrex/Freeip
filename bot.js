const TelegramBot = require('node-telegram-bot-api');
const mongoose = require('mongoose');
const axios = require('axios');
const Session = require('./models/Session');
const Job = require('./models/Job');

const TOKEN = process.env.BOT_TOKEN;
const WEBHOOK_URL = process.env.WEBHOOK_URL; // e.g. https://your-app.onrender.com
const PORT = process.env.PORT || 3000;

// ── Connect MongoDB ──────────────────────────────────────────────
mongoose.connect(process.env.MONGODB_URI)
  .then(() => console.log('✅ MongoDB connected'))
  .catch(err => console.error('❌ MongoDB error:', err));

// ── Bot init (webhook mode) ──────────────────────────────────────
const bot = new TelegramBot(TOKEN);
bot.setWebHook(`${WEBHOOK_URL}/bot${TOKEN}`);

// ── Express for webhook + self-ping ─────────────────────────────
const express = require('express');
const app = express();
app.use(express.json());

app.post(`/bot${TOKEN}`, (req, res) => {
  bot.processUpdate(req.body);
  res.sendStatus(200);
});

app.get('/ping', (_req, res) => res.send('pong'));

app.listen(PORT, () => console.log(`🚀 Server on port ${PORT}`));

// ── Self-ping every 10 minutes ───────────────────────────────────
setInterval(async () => {
  try {
    await axios.get(`${WEBHOOK_URL}/ping`);
    console.log(`[${new Date().toISOString()}] 🏓 Self-ping OK`);
  } catch (e) {
    console.error('Self-ping failed:', e.message);
  }
}, 10 * 60 * 1000);

// ── Regex ────────────────────────────────────────────────────────
const URL_REGEX = /https?:\/\/[^\s]+/i;
const IPV4_REGEX = /^(\d{1,3}\.){3}\d{1,3}(:\d+)?(\/\S*)?$/;

function isValidTarget(text) {
  return URL_REGEX.test(text) || IPV4_REGEX.test(text.trim());
}

function normalizeTarget(text) {
  const t = text.trim();
  if (URL_REGEX.test(t)) return t.match(URL_REGEX)[0];
  return t.startsWith('http') ? t : `http://${t}`;
}

// ── State machine steps ──────────────────────────────────────────
const STEP = { IDLE: 'idle', WAIT_COUNT: 'wait_count', WAIT_DELAY: 'wait_delay' };

// ── /start ───────────────────────────────────────────────────────
bot.onText(/\/start/, async (msg) => {
  const chatId = msg.chat.id;
  await Session.findOneAndUpdate(
    { chatId },
    { step: STEP.IDLE, target: null, count: null, delay: null },
    { upsert: true }
  );
  bot.sendMessage(chatId,
    `👋 *HTTP Request Sender Bot*\n\nSend me a URL or IPv4 address to get started.`,
    { parse_mode: 'Markdown' }
  );
});

// ── Main message handler ─────────────────────────────────────────
bot.on('message', async (msg) => {
  if (!msg.text || msg.text.startsWith('/')) return;

  const chatId = msg.chat.id;
  let session = await Session.findOne({ chatId });
  if (!session) {
    session = await Session.create({ chatId, step: STEP.IDLE });
  }

  // ── Step 1: user sends target ──────────────────────────────────
  if (session.step === STEP.IDLE) {
    if (!isValidTarget(msg.text)) {
      return bot.sendMessage(chatId, '❌ Please send a valid URL or IPv4 address.');
    }
    const target = normalizeTarget(msg.text);
    await Session.findOneAndUpdate({ chatId }, { target, step: STEP.WAIT_COUNT });
    return bot.sendMessage(chatId,
      `✅ Target set:\n\`${target}\`\n\n📊 How many requests? *(max 100)*`,
      { parse_mode: 'Markdown' }
    );
  }

  // ── Step 2: user sends count ───────────────────────────────────
  if (session.step === STEP.WAIT_COUNT) {
    const count = parseInt(msg.text, 10);
    if (isNaN(count) || count < 1 || count > 100) {
      return bot.sendMessage(chatId, '❌ Please enter a number between 1 and 100.');
    }
    await Session.findOneAndUpdate({ chatId }, { count, step: STEP.WAIT_DELAY });
    return bot.sendMessage(chatId,
      `✅ Requests: *${count}*\n\n⏱ Send interval in seconds? *(max 3600)*`,
      { parse_mode: 'Markdown' }
    );
  }

  // ── Step 3: user sends delay ───────────────────────────────────
  if (session.step === STEP.WAIT_DELAY) {
    const delay = parseInt(msg.text, 10);
    if (isNaN(delay) || delay < 0 || delay > 3600) {
      return bot.sendMessage(chatId, '❌ Please enter seconds between 0 and 3600.');
    }
    await Session.findOneAndUpdate({ chatId }, { delay, step: STEP.IDLE });

    const s = await Session.findOne({ chatId });
    const totalTime = s.count * delay;
    const totalStr = totalTime >= 60
      ? `~${Math.round(totalTime / 60)} min`
      : `~${totalTime}s`;

    return bot.sendMessage(chatId,
      `📋 *Summary*\n\n🌐 Target: \`${s.target}\`\n📦 Requests: *${s.count}*\n⏱ Interval: *${delay}s*\n🕒 Est. time: *${totalStr}*\n\nReady to fire?`,
      {
        parse_mode: 'Markdown',
        reply_markup: {
          inline_keyboard: [[
            { text: '🚀 Start Sending', callback_data: `start_${chatId}` }
          ]]
        }
      }
    );
  }
});

// ── Callback: Start / Stats ──────────────────────────────────────
bot.on('callback_query', async (query) => {
  const chatId = query.message.chat.id;
  const msgId = query.message.message_id;
  const data = query.data;

  await bot.answerCallbackQuery(query.id);

  // ── Start button ───────────────────────────────────────────────
  if (data.startsWith('start_')) {
    const session = await Session.findOne({ chatId });
    if (!session || !session.target) {
      return bot.sendMessage(chatId, '❌ Session expired. Send a new target.');
    }

    // Create job
    const job = await Job.create({
      chatId,
      target: session.target,
      totalCount: session.count,
      delay: session.delay,
      sent: 0,
      errors: 0,
      status: 'running',
      startedAt: new Date()
    });

    // Edit original message
    await bot.editMessageText(
      `🚀 *Sending started!*\n\n🌐 \`${session.target}\`\n📦 ${session.count} requests | ⏱ ${session.delay}s apart`,
      {
        chat_id: chatId,
        message_id: msgId,
        parse_mode: 'Markdown',
        reply_markup: {
          inline_keyboard: [[
            { text: '📊 Stats', callback_data: `stats_${job._id}` }
          ]]
        }
      }
    );

    // Run requests in background
    runRequests(job._id, chatId);
  }

  // ── Stats button ───────────────────────────────────────────────
  if (data.startsWith('stats_')) {
    const jobId = data.replace('stats_', '');
    const job = await Job.findById(jobId);
    if (!job) return bot.sendMessage(chatId, '❌ Job not found.');

    const remaining = job.totalCount - job.sent - job.errors;
    const pct = Math.round((job.sent / job.totalCount) * 100);
    const bar = buildBar(pct);
    const elapsed = Math.round((Date.now() - job.startedAt) / 1000);
    const statusEmoji = job.status === 'running' ? '🟢' : job.status === 'done' ? '✅' : '❌';

    bot.sendMessage(chatId,
      `📊 *Job Stats*\n\n${statusEmoji} Status: *${job.status}*\n🌐 Target: \`${job.target}\`\n\n` +
      `${bar} ${pct}%\n` +
      `✅ Sent: *${job.sent}*\n` +
      `❌ Errors: *${job.errors}*\n` +
      `⏳ Remaining: *${Math.max(0, remaining)}*\n` +
      `📦 Total: *${job.totalCount}*\n` +
      `🕒 Elapsed: *${elapsed}s*`,
      {
        parse_mode: 'Markdown',
        reply_markup: job.status === 'running' ? {
          inline_keyboard: [[
            { text: '🔄 Refresh Stats', callback_data: `stats_${job._id}` }
          ]]
        } : undefined
      }
    );
  }
});

// ── Progress bar helper ──────────────────────────────────────────
function buildBar(pct) {
  const filled = Math.round(pct / 10);
  return '█'.repeat(filled) + '░'.repeat(10 - filled);
}

// ── Background request runner ────────────────────────────────────
async function runRequests(jobId, chatId) {
  const job = await Job.findById(jobId);
  if (!job) return;

  for (let i = 0; i < job.totalCount; i++) {
    // Re-fetch in case job was cancelled (future feature)
    const current = await Job.findById(jobId);
    if (!current || current.status !== 'running') break;

    try {
      await axios.get(current.target, { timeout: 10000 });
      await Job.findByIdAndUpdate(jobId, { $inc: { sent: 1 } });
    } catch (_e) {
      await Job.findByIdAndUpdate(jobId, { $inc: { errors: 1 } });
    }

    // Wait delay (skip after last request)
    if (i < job.totalCount - 1 && job.delay > 0) {
      await sleep(job.delay * 1000);
    }
  }

  // Mark done
  const final = await Job.findByIdAndUpdate(jobId, { status: 'done' }, { new: true });
  const bar = buildBar(100);

  bot.sendMessage(chatId,
    `🎉 *All done!*\n\n` +
    `${bar} 100%\n` +
    `✅ Sent: *${final.sent}*\n` +
    `❌ Errors: *${final.errors}*\n` +
    `📦 Total: *${final.totalCount}*\n` +
    `🌐 Target: \`${final.target}\``,
    { parse_mode: 'Markdown' }
  );
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}
