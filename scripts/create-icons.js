'use strict';
const fs   = require('fs');
const path = require('path');
const zlib = require('zlib');

const CRC_TABLE = new Uint32Array(256);
for (let n = 0; n < 256; n++) {
  let c = n;
  for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
  CRC_TABLE[n] = c;
}
function crc32(buf) {
  let crc = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) crc = CRC_TABLE[(crc ^ buf[i]) & 0xFF] ^ (crc >>> 8);
  return (crc ^ 0xFFFFFFFF) >>> 0;
}
function pngChunk(type, data) {
  const t = Buffer.from(type, 'ascii');
  const l = Buffer.alloc(4); l.writeUInt32BE(data.length);
  const c = Buffer.alloc(4); c.writeUInt32BE(crc32(Buffer.concat([t, data])));
  return Buffer.concat([l, t, data, c]);
}
function makePNG(w, h, px) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; ihdr[9] = 6; // 8-bit RGBA
  const raw = Buffer.alloc(h * (1 + w * 4));
  for (let y = 0; y < h; y++) {
    raw[y * (1 + w * 4)] = 0;
    for (let x = 0; x < w; x++) {
      const s = (y * w + x) * 4, d = y * (1 + w * 4) + 1 + x * 4;
      raw[d] = px[s]; raw[d+1] = px[s+1]; raw[d+2] = px[s+2]; raw[d+3] = px[s+3];
    }
  }
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', zlib.deflateSync(raw, { level: 9 })),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

function drawIcon(size, bgR, bgG, bgB, fgR, fgG, fgB) {
  const px = Buffer.alloc(size * size * 4, 0);
  const cx = size / 2, cy = size / 2;
  const outerR = size / 2 - 1;

  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = (x + 0.5) - cx, dy = (y + 0.5) - cy;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const i = (y * size + x) * 4;

      let alpha = 0;
      if (dist <= outerR - 0.5) alpha = 255;
      else if (dist <= outerR + 0.5) alpha = Math.round((outerR + 0.5 - dist) * 255);

      if (alpha > 0) {
        px[i] = bgR; px[i+1] = bgG; px[i+2] = bgB; px[i+3] = alpha;
      }
    }
  }

  // Draw a simple "D" in the foreground colour
  const unit = size / 22;
  const barL = Math.round(5 * unit), barR = Math.round(8 * unit);
  const topY  = Math.round(4 * unit), botY = Math.round(18 * unit);
  const arcCX = Math.round(8 * unit), arcCY = (topY + botY) / 2;
  const arcRX = size / 2 - 3 * unit, arcRY = (botY - topY) / 2;
  const stroke = Math.max(1, Math.round(unit * 1.5));

  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      if (px[i + 3] === 0) continue;

      const py = y + 0.5, px2 = x + 0.5;
      let isFg = false;

      // Vertical bar
      if (px2 >= barL && px2 <= barR && py >= topY && py <= botY) isFg = true;

      // Top cap
      if (py >= topY && py <= topY + stroke && px2 >= barL && px2 <= arcCX + arcRX) isFg = true;

      // Bottom cap
      if (py >= botY - stroke && py <= botY && px2 >= barL && px2 <= arcCX + arcRX) isFg = true;

      // Curve of D — points on/near the right semi-ellipse
      if (px2 >= arcCX) {
        const nx = (px2 - arcCX) / arcRX, ny = (py - arcCY) / arcRY;
        const d = Math.sqrt(nx * nx + ny * ny);
        if (Math.abs(d - 1.0) * Math.min(arcRX, arcRY) <= stroke) isFg = true;
      }

      if (isFg) { px[i] = fgR; px[i+1] = fgG; px[i+2] = fgB; px[i+3] = 255; }
    }
  }
  return px;
}

function makeIco(pngBuf) {
  const header = Buffer.alloc(6);
  header[2] = 1; header[4] = 1; // type=ICO, count=1
  const entry = Buffer.alloc(16);
  // width=0 and height=0 both mean 256 in ICO format
  entry.writeUInt16LE(1, 4);  // planes
  entry.writeUInt16LE(32, 6); // bit count
  entry.writeUInt32LE(pngBuf.length, 8);
  entry.writeUInt32LE(22, 12); // offset = 6 + 16
  return Buffer.concat([header, entry, pngBuf]);
}

const ASSETS = path.join(__dirname, '..', 'assets');
if (!fs.existsSync(ASSETS)) fs.mkdirSync(ASSETS, { recursive: true });

// purple #6d28d9 = rgb(109, 40, 217)   lighter #8b5cf6 = rgb(139, 92, 246)
const icon256 = drawIcon(256, 109, 40, 217, 255, 255, 255);
const icon22n = drawIcon(22,  109, 40, 217, 255, 255, 255);
const icon22s = drawIcon(22,  139, 92, 246, 255, 255, 255);

const png256 = makePNG(256, 256, icon256);
fs.writeFileSync(path.join(ASSETS, 'icon.png'),               png256);
fs.writeFileSync(path.join(ASSETS, 'icon.ico'),               makeIco(png256));
fs.writeFileSync(path.join(ASSETS, 'tray-icon.png'),          makePNG(22, 22, icon22n));
fs.writeFileSync(path.join(ASSETS, 'tray-icon-syncing.png'),  makePNG(22, 22, icon22s));

console.log('assets/ icons created.');
