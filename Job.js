const mongoose = require('mongoose');

const jobSchema = new mongoose.Schema({
  chatId:     { type: Number, required: true },
  target:     { type: String, required: true },
  totalCount: { type: Number, required: true },
  delay:      { type: Number, required: true },   // seconds between requests
  sent:       { type: Number, default: 0 },
  errors:     { type: Number, default: 0 },
  status:     { type: String, default: 'running' }, // running | done | failed
  startedAt:  { type: Date,   default: Date.now },
}, { timestamps: true });

module.exports = mongoose.model('Job', jobSchema);
