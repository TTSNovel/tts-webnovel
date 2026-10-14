/*
 * "VietPhrase" translation engine — converts Chinese text to its
 * Sino-Vietnamese (Hán Việt) reading, character by character, e.g.
 * 第九章 -> "Đệ cửu chương". Same phienam.txt table (and the same idea) as
 * tools/hanviet.py, ported to the browser so reader.js can convert a
 * chapter client-side with no server round trip — the table is fetched
 * once (~110KB, lazily, only when a chapter actually needs converting)
 * and kept in memory for the rest of the page's life.
 *
 * Kept in step with the iOS/macOS app's VietPhraseTranslationEngine
 * (tts-novel-ios, Packages/Core/Sources/Core/Translation/) — same spacing,
 * punctuation and capitalization rules, so a chapter reads the same on
 * both.
 *
 * This is a literal 1-1 phonetic transliteration, not a phrase-level
 * translation (no VietPhrase.txt/Names.txt phrase dictionaries) — word
 * order and grammar stay Chinese.
 */
(function () {
  'use strict';

  const DICT_URL = '/assets/phienam.txt';
  const HAN_RE = /\p{Script=Han}/u;
  const LATIN_RE = /\p{Script=Latin}/u;

  // Full-width CJK punctuation -> its Vietnamese/ASCII equivalent, so the
  // converted text reads naturally and reader.js's sentence splitter (which
  // only knows [.!?]) can actually split it into sentences for TTS.
  const PUNCT = {
    '。': '.', '．': '.', '，': ',', '、': ',', '！': '!', '？': '?', '：': ':', '；': ';',
    '（': '(', '）': ')', '【': '[', '】': ']', '《': '“', '》': '”', '〈': '“', '〉': '”',
    '「': '“', '」': '”', '『': '“', '』': '”', '～': '~', '　': ' ',
  };
  // A Han reading glues directly onto these (no space in between) — they
  // open a bracket/quote, or are already whitespace.
  const OPENERS = new Set([' ', '\n', '\t', '“', '‘', '(', '[', '"', "'"]);
  const SENTENCE_END = new Set(['.', '!', '?', '…']);

  let table = null;
  let loadPromise = null;

  function parseTable(text) {
    const map = new Map();
    for (const line of text.split('\n')) {
      const eq = line.indexOf('=');
      if (eq <= 0) continue;
      const reading = line.slice(eq + 1).trim();
      if (reading) map.set(line.slice(0, eq), reading);
    }
    return map;
  }

  function load() {
    if (table) return Promise.resolve();
    if (!loadPromise) {
      loadPromise = fetch(DICT_URL)
        .then((r) => {
          if (!r.ok) throw new Error(`Tải từ điển Hán Việt lỗi: ${r.status}`);
          return r.text();
        })
        .then((text) => {
          table = parseTable(text);
        })
        .catch((err) => {
          loadPromise = null; // let the next attempt retry
          throw err;
        });
    }
    return loadPromise;
  }

  function capitalize(word) {
    return word.charAt(0).toUpperCase() + word.slice(1);
  }

  // Requires load() to have resolved. Han characters with no entry in the
  // table pass through unchanged, as does everything that isn't Han.
  function convert(text) {
    if (!table || !HAN_RE.test(text)) return text;
    let out = '';
    // Whether the last thing appended was a reading / a (mapped) Chinese
    // punctuation mark — Chinese runs words and punctuation together with
    // no spaces, Vietnamese doesn't, so a word, number or opening quote
    // right after either needs one ("đệ1chương" -> "đệ 1 chương",
    // 力量：1.2 -> "lực lượng: 1.2", 他说：“…” -> "tha thuyết: “…”").
    let lastWasReading = false;
    let lastWasCjkPunct = false;
    // Capitalize the first reading of each sentence — start of text, right
    // after a sentence-ending mark, or at the start of quoted speech.
    let sentenceStart = true;
    for (const raw of text) {
      const reading = table.get(raw);
      if (reading) {
        const prev = out.charAt(out.length - 1);
        if (out && !OPENERS.has(prev)) out += ' ';
        out += sentenceStart ? capitalize(reading) : reading;
        sentenceStart = false;
        lastWasReading = true;
        lastWasCjkPunct = false;
        continue;
      }
      const ch = PUNCT[raw] ?? raw;
      const isWordStart = /[\p{L}\p{N}]/u.test(ch) || ch === '“';
      if ((lastWasReading || lastWasCjkPunct) && isWordStart) out += ' ';
      out += ch;
      lastWasReading = false;
      lastWasCjkPunct = (ch !== raw && ch !== ' ') || raw === '”';
      if (SENTENCE_END.has(ch) || ch === '“') sentenceStart = true;
      else if (/[\p{L}\p{N}]/u.test(ch)) sentenceStart = false;
    }
    return out;
  }

  // Whether `text` is predominantly Chinese — the web counterpart of the
  // app's NLLanguageRecognizer check: a Vietnamese chapter with a stray
  // leftover Han character or two shouldn't offer to "translate".
  function needsTranslation(text) {
    let han = 0;
    let latin = 0;
    for (const ch of text) {
      if (HAN_RE.test(ch)) han++;
      else if (LATIN_RE.test(ch)) latin++;
    }
    // A Han character carries a whole syllable, a Latin letter a fraction
    // of one — weight accordingly so a mostly-Chinese chapter with some
    // English names still counts.
    return han > 0 && han * 3 >= latin;
  }

  window.HanViet = { load, convert, needsTranslation };
})();
