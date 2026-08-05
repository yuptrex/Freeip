const mongoose = require('mongoose');

const sessionSchema = new mongoose.Schema({
  chatId:  { type: Number, required: true, unique: true },
  step:    { type: String, default: 'idle' },
  target:  { type: String, default: null },
  count:   { type: Number, default: null },
  delay:   { type: Number, default: null },
}, { timestamps: true });

module.exports = mongoose.model('Session', sessionSchema);
