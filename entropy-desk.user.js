// ==UserScript==
// @name         Entropy Desk · ANTH / SNDK
// @namespace    entropy-desk
// @version      1.5.8
// @description  ALO-quote io:ANTH / io:SNDK; flatten reduce-only while in position
// @match        https://entropy.io/*
// @match        https://app.hyperliquid.xyz/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(function () {
  "use strict";
  if (window.__entropyDeskLoaded) return;
  window.__entropyDeskLoaded = true;
  var INFO_URL = "https://api.hyperliquid.xyz/info";
  var WS_URL = "wss://api.hyperliquid.xyz/ws";
  var ALLOWED = ["io:ANTH", "io:SNDK"];
  var DEX = "io";
  var POLL_MS = 400;
  var EXCHANGE_URL = "https://api.hyperliquid.xyz/exchange";
  var BUILDER = "0xcD254d2A328f7f67C7c6FEf930A4757516F7b601";
  var BUILDER_LC = BUILDER.toLowerCase();
  var ZERO = "0x0000000000000000000000000000000000000000";
  var FEE = {
    hlTier: 4,
    hlTaker: 0.00028,
    hlMaker: 0,
    deployerFeeScale: 1,
    hip3Scale: 2,
    growthMode: 0.1,
    entropyShare: 0.5,
    selfRebateMult: 2,
    netTaker: 0,
    netMaker: 0
  };
  var state = {
    dexIndex: null,
    assets: {},
    quotes: {},
    paperLog: [],
    liveEnabled: true,
    notional: 50,
    wallet: "",
    agentWallet: null,
    agentAddress: "",
    agentApproved: null,
    lastErr: "",
    lastOk: "",
    polling: false,
    inFlight: false,
    autoMm: false,
    autoCoins: { "io:ANTH": false, "io:SNDK": false },
    autoBusy: false,
    lastNonce: 0,
    oids: { "io:ANTH": { buy: null, sell: null }, "io:SNDK": { buy: null, sell: null } },
    extraOids: { "io:ANTH": [], "io:SNDK": [] },
    lastQuoted: { "io:ANTH": "", "io:SNDK": "" },
    needsRequote: { "io:ANTH": false, "io:SNDK": false },
    requoteBusy: { "io:ANTH": false, "io:SNDK": false },
    ws: null,
    wsUp: false,
    wsRetry: 0,
    wsPing: 0,
    wsLastMsg: 0,
    wsOrderUser: "",
    lastDeadMan: 0,
    lastReconcile: 0,
    pos: { "io:ANTH": 0, "io:SNDK": 0 },
    posTs: 0,
    posForce: false
  };
  function anyAutoCoin() {
    for (var i = 0; i < ALLOWED.length; i++) {
      if (state.autoCoins[ALLOWED[i]]) return true;
    }
    return false;
  }
  function syncAutoMm() {
    state.autoMm = anyAutoCoin();
    return state.autoMm;
  }
  function autoOn(coin) {
    return !!(state.autoCoins && state.autoCoins[coin]);
  }
  function log() {
    var args = ["[Entropy Desk]"].concat([].slice.call(arguments));
    console.log.apply(console, args);
  }
  function warn() {
    var args = ["[Entropy Desk]"].concat([].slice.call(arguments));
    console.warn.apply(console, args);
  }
  function nowMs() { return Date.now(); }
  function shortAddr(a) {
    if (!a) return "未连接";
    var s = String(a);
    return s.length < 12 ? s : s.slice(0, 6) + "…" + s.slice(-4);
  }
  function printFees() {
    var protoTaker = FEE.hlTaker * FEE.hip3Scale * FEE.growthMode;
    var rebate = protoTaker * FEE.entropyShare * FEE.selfRebateMult;
    var lines = [
      "费率说明 / fee stack",
      "  HL 成交量档 4：taker 0.028%  maker 0",
      "  HIP-3 deployerFeeScale=1 → 倍率 scale 2  → taker 0.056%",
      "  Growth Mode ×0.1 → taker 0.0056%",
      "  Entropy 自返 200% × Entropy 的 50% 份额 = 0.0056%",
      "  净 taker ≈ 0    maker 0",
      "  验算: 0.028% × 2 × 0.1 = 0.0056%; rebate 0.0056% → net " + ((protoTaker - rebate) * 100).toFixed(6) + "%"
    ];
    log(lines.join("\n"));
    return lines;
  }
  function assertAllowedCoin(coin) {
    var c = String(coin || "");
    if (/^(xyz|vntl):/i.test(c)) throw new Error("拒绝 xyz: / vntl: 市场，仅 io");
    if (ALLOWED.indexOf(c) === -1) throw new Error("仅支持 io:ANTH / io:SNDK，收到: " + c);
    if (c.split(":")[0] !== DEX) throw new Error("DEX 必须为 io");
    return c;
  }
  function fmtNum(n, d) {
    var x = Number(n);
    if (!Number.isFinite(x)) return "—";
    return x.toFixed(d == null ? 4 : d);
  }
  function fmtUsd(n) {
    var x = Number(n);
    if (!Number.isFinite(x)) return "—";
    var ax = Math.abs(x);
    if (ax >= 1e6) return (x / 1e6).toFixed(2) + "M";
    if (ax >= 1e4) return (x / 1e4).toFixed(2) + "万";
    if (ax >= 1e3) return (x / 1e3).toFixed(2) + "k";
    return x.toFixed(2);
  }
  function entropyWallet() {
    try {
      var w = window.entropy && window.entropy.user && window.entropy.user.walletAddress;
      if (w) return String(w);
    } catch (e0) {}
    return "";
  }
  function currentWallet() { return state.wallet || entropyWallet() || ""; }
  function u8concat(chunks) {
    var n = 0, i, o = 0;
    for (i = 0; i < chunks.length; i++) n += chunks[i].length;
    var out = new Uint8Array(n);
    for (i = 0; i < chunks.length; i++) { out.set(chunks[i], o); o += chunks[i].length; }
    return out;
  }
  function be(n, bytes) {
    var a = new Uint8Array(bytes);
    var x = n;
    for (var i = bytes - 1; i >= 0; i--) { a[i] = x & 0xff; x = Math.floor(x / 256); }
    return a;
  }
  function beBig(n, bytes) {
    var a = new Uint8Array(bytes);
    var x = BigInt(n);
    for (var i = bytes - 1; i >= 0; i--) { a[i] = Number(x & 255n); x >>= 8n; }
    return a;
  }
  function mpEncode(val) {
    if (val === null || val === undefined) return new Uint8Array([0xc0]);
    if (val === true) return new Uint8Array([0xc3]);
    if (val === false) return new Uint8Array([0xc2]);
    var t = typeof val;
    if (t === "number") {
      if (!Number.isFinite(val) || !Number.isInteger(val)) throw new Error("msgpack 仅编码整数: " + val);
      return mpInt(val);
    }
    if (t === "string") return mpStr(val);
    if (Array.isArray(val)) return mpArr(val);
    if (t === "object") return mpMap(val);
    throw new Error("msgpack 不支持类型 " + t);
  }
  function mpInt(n) {
    if (n >= 0) {
      if (n <= 0x7f) return new Uint8Array([n]);
      if (n <= 0xff) return u8concat([new Uint8Array([0xcc]), be(n, 1)]);
      if (n <= 0xffff) return u8concat([new Uint8Array([0xcd]), be(n, 2)]);
      if (n <= 0xffffffff) return u8concat([new Uint8Array([0xce]), be(n, 4)]);
      return u8concat([new Uint8Array([0xcf]), beBig(n, 8)]);
    }
    if (n >= -32) return new Uint8Array([n & 0xff]);
    if (n >= -128) return u8concat([new Uint8Array([0xd0]), new Uint8Array([n & 0xff])]);
    if (n >= -32768) return u8concat([new Uint8Array([0xd1]), be(n, 2)]);
    if (n >= -2147483648) return u8concat([new Uint8Array([0xd2]), be(n, 4)]);
    return u8concat([new Uint8Array([0xd3]), beBig(n, 8)]);
  }
  function mpStr(s) {
    var b = new TextEncoder().encode(s);
    var n = b.length;
    var hdr;
    if (n <= 31) hdr = new Uint8Array([0xa0 | n]);
    else if (n <= 0xff) hdr = u8concat([new Uint8Array([0xd9]), be(n, 1)]);
    else if (n <= 0xffff) hdr = u8concat([new Uint8Array([0xda]), be(n, 2)]);
    else hdr = u8concat([new Uint8Array([0xdb]), be(n, 4)]);
    return u8concat([hdr, b]);
  }
  function mpArr(arr) {
    var n = arr.length;
    var hdr;
    if (n <= 15) hdr = new Uint8Array([0x90 | n]);
    else if (n <= 0xffff) hdr = u8concat([new Uint8Array([0xdc]), be(n, 2)]);
    else hdr = u8concat([new Uint8Array([0xdd]), be(n, 4)]);
    var parts = [hdr];
    for (var i = 0; i < n; i++) parts.push(mpEncode(arr[i]));
    return u8concat(parts);
  }
  function mpMap(obj) {
    var keys = Object.keys(obj);
    var n = keys.length;
    var hdr;
    if (n <= 15) hdr = new Uint8Array([0x80 | n]);
    else if (n <= 0xffff) hdr = u8concat([new Uint8Array([0xde]), be(n, 2)]);
    else hdr = u8concat([new Uint8Array([0xdf]), be(n, 4)]);
    var parts = [hdr];
    for (var i = 0; i < n; i++) { parts.push(mpStr(keys[i])); parts.push(mpEncode(obj[keys[i]])); }
    return u8concat(parts);
  }
  function connectionPayload(action, nonce) {
    return u8concat([mpEncode(action), beBig(nonce, 8), new Uint8Array([0x00])]);
  }
  function floatToWire(x) {
    var n = Number(x);
    if (!Number.isFinite(n)) throw new Error("非法价格/数量: " + x);
    var rounded = n.toFixed(8);
    if (Math.abs(Number(rounded) - n) >= 1e-12) throw new Error("float_to_wire 舍入失败: " + x);
    if (rounded === "-0.00000000") rounded = "0.00000000";
    var s = rounded;
    if (s.indexOf(".") !== -1) s = s.replace(/\.?0+$/, "");
    if (s === "-0" || s === "") s = "0";
    return s;
  }
  function roundPx(px, szDecimals) {
    var n = Number(px);
    if (!Number.isFinite(n) || n <= 0) throw new Error("非法价格");
    var maxDec = Math.max(0, 6 - (szDecimals | 0));
    var sig = Number(n.toPrecision(5));
    var fac = Math.pow(10, maxDec);
    return floatToWire(Math.round(sig * fac) / fac);
  }
  function roundSz(sz, szDecimals) {
    var n = Number(sz);
    var fac = Math.pow(10, szDecimals | 0);
    var limited = Math.round(n * fac) / fac;
    if (!(limited > 0)) throw new Error("数量过小");
    return floatToWire(limited);
  }
  function tickSize(px, szDecimals) {
    var wired = roundPx(px, szDecimals);
    var s = wired.indexOf(".") === -1 ? 0 : wired.split(".")[1].length;
    return Math.pow(10, -s);
  }
  function rotl64(x, n) {
    n = BigInt(n);
    return ((x << n) | (x >> (64n - n))) & 0xffffffffffffffffn;
  }
  function keccak256(bytes) {
    var RC = [0x0000000000000001n,0x0000000000008082n,0x800000000000808an,0x8000000080008000n,0x000000000000808bn,0x0000000080000001n,0x8000000080008081n,0x8000000000008009n,0x000000000000008an,0x0000000000000088n,0x0000000080008009n,0x000000008000000an,0x000000008000808bn,0x800000000000008bn,0x8000000000008089n,0x8000000000008003n,0x8000000000008002n,0x8000000000000080n,0x000000000000800an,0x800000008000000an,0x8000000080008081n,0x8000000000008080n,0x0000000080000001n,0x8000000080008008n];
    var st = [];
    var i, r, x, y, t;
    for (i = 0; i < 25; i++) st[i] = 0n;
    var rate = 136;
    var buf = new Uint8Array(rate);
    var off = 0;
    function absorbBlock() {
      var bi, lane, b, z;
      for (bi = 0; bi < rate / 8; bi++) {
        lane = 0n;
        for (b = 0; b < 8; b++) lane |= BigInt(buf[bi * 8 + b]) << BigInt(8 * b);
        st[bi] ^= lane;
      }
      keccakF(st, RC);
      for (z = 0; z < rate; z++) buf[z] = 0;
    }
    for (i = 0; i < bytes.length; i++) {
      buf[off++] = bytes[i];
      if (off === rate) { absorbBlock(); off = 0; }
    }
    buf[off] ^= 0x01;
    buf[rate - 1] ^= 0x80;
    absorbBlock();
    var out = new Uint8Array(32);
    for (i = 0; i < 4; i++) {
      var lane2 = st[i];
      for (var b2 = 0; b2 < 8; b2++) out[i * 8 + b2] = Number((lane2 >> BigInt(8 * b2)) & 255n);
    }
    return out;
  }
  function keccakF(st, RC) {
    var RHO = [0,1,62,28,27,36,44,6,55,20,3,10,43,25,39,41,45,15,21,8,18,2,61,56,14];
    var C = new Array(5);
    var D = new Array(5);
    var B = new Array(25);
    for (var round = 0; round < 24; round++) {
      var x, y, i;
      for (x = 0; x < 5; x++) C[x] = st[x] ^ st[x + 5] ^ st[x + 10] ^ st[x + 15] ^ st[x + 20];
      for (x = 0; x < 5; x++) D[x] = C[(x + 4) % 5] ^ rotl64(C[(x + 1) % 5], 1);
      for (i = 0; i < 25; i++) st[i] ^= D[i % 5];
      for (x = 0; x < 5; x++) {
        for (y = 0; y < 5; y++) {
          i = x + 5 * y;
          B[y + 5 * ((2 * x + 3 * y) % 5)] = rotl64(st[i], RHO[i]);
        }
      }
      for (x = 0; x < 5; x++) {
        for (y = 0; y < 5; y++) {
          i = x + 5 * y;
          st[i] = (B[i] ^ ((~B[((x + 1) % 5) + 5 * y]) & B[((x + 2) % 5) + 5 * y])) & 0xffffffffffffffffn;
        }
      }
      st[0] = (st[0] ^ RC[round]) & 0xffffffffffffffffn;
    }
  }
  function toHex32(u8) {
    var s = "0x";
    for (var i = 0; i < u8.length; i++) s += (u8[i] + 256).toString(16).slice(-2);
    return s;
  }
  function hexToBytes(h) {
    var s = String(h).slice(0, 2) === "0x" ? String(h).slice(2) : String(h);
    var out = new Uint8Array(s.length / 2);
    for (var i = 0; i < out.length; i++) out[i] = parseInt(s.slice(i * 2, i * 2 + 2), 16);
    return out;
  }
  var nobleSecp = (function () {
    var module = { exports: {} };
    var exports = module.exports;
    var define = undefined;
    function require(name) {
      if (name === "crypto") return {};
      throw new Error("isolated require blocked: " + name);
    }
/**
 * Minified by jsDelivr using Terser v5.39.0.
 * Original file: /npm/@noble/secp256k1@1.7.1/lib/index.js
 *
 * Do NOT use SRI with dynamically generated files! More information: https://www.jsdelivr.com/using-sri-with-dynamic-files
 */
"use strict";
/*! noble-secp256k1 - MIT License (c) 2019 Paul Miller (paulmillr.com) */Object.defineProperty(exports,"__esModule",{value:!0}),exports.utils=exports.schnorr=exports.verify=exports.signSync=exports.sign=exports.getSharedSecret=exports.recoverPublicKey=exports.getPublicKey=exports.Signature=exports.Point=exports.CURVE=void 0;const nodeCrypto=require("crypto"),_0n=BigInt(0),_1n=BigInt(1),_2n=BigInt(2),_3n=BigInt(3),_8n=BigInt(8),CURVE=Object.freeze({a:_0n,b:BigInt(7),P:BigInt("0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f"),n:BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"),h:_1n,Gx:BigInt("55066263022277343669578718895168534326250603453777594175500187360389116729240"),Gy:BigInt("32670510020758816978083085130507043184471273380659243275938904335757337482424"),beta:BigInt("0x7ae96a2b657c07106e64479eac3434e99cf0497512f58995c1396c28719501ee")});exports.CURVE=CURVE;const divNearest=(t,e)=>(t+e/_2n)/e,endo={beta:BigInt("0x7ae96a2b657c07106e64479eac3434e99cf0497512f58995c1396c28719501ee"),splitScalar(t){const{n:e}=CURVE,n=BigInt("0x3086d221a7d46bcde86c90e49284eb15"),r=-_1n*BigInt("0xe4437ed6010e88286f547fa90abfe4c3"),i=BigInt("0x114ca50f7a8e2f3f657c1108d9d44cfd8"),o=n,s=BigInt("0x100000000000000000000000000000000"),a=divNearest(o*t,e),c=divNearest(-r*t,e);let u=mod(t-a*n-c*i,e),f=mod(-a*r-c*o,e);const h=u>s,y=f>s;if(h&&(u=e-u),y&&(f=e-f),u>s||f>s)throw new Error("splitScalarEndo: Endomorphism failed, k="+t);return{k1neg:h,k1:u,k2neg:y,k2:f}}},fieldLen=32,groupLen=32,hashLen=32,compressedLen=33,uncompressedLen=65;function weierstrass(t){const{a:e,b:n}=CURVE,r=mod(t*t),i=mod(r*t);return mod(i+e*t+n)}const USE_ENDOMORPHISM=CURVE.a===_0n;class ShaError extends Error{constructor(t){super(t)}}function assertJacPoint(t){if(!(t instanceof JacobianPoint))throw new TypeError("JacobianPoint expected")}class JacobianPoint{constructor(t,e,n){this.x=t,this.y=e,this.z=n}static fromAffine(t){if(!(t instanceof Point))throw new TypeError("JacobianPoint#fromAffine: expected Point");return t.equals(Point.ZERO)?JacobianPoint.ZERO:new JacobianPoint(t.x,t.y,_1n)}static toAffineBatch(t){const e=invertBatch(t.map((t=>t.z)));return t.map(((t,n)=>t.toAffine(e[n])))}static normalizeZ(t){return JacobianPoint.toAffineBatch(t).map(JacobianPoint.fromAffine)}equals(t){assertJacPoint(t);const{x:e,y:n,z:r}=this,{x:i,y:o,z:s}=t,a=mod(r*r),c=mod(s*s),u=mod(e*c),f=mod(i*a),h=mod(mod(n*s)*c),y=mod(mod(o*r)*a);return u===f&&h===y}negate(){return new JacobianPoint(this.x,mod(-this.y),this.z)}double(){const{x:t,y:e,z:n}=this,r=mod(t*t),i=mod(e*e),o=mod(i*i),s=t+i,a=mod(_2n*(mod(s*s)-r-o)),c=mod(_3n*r),u=mod(c*c),f=mod(u-_2n*a),h=mod(c*(a-f)-_8n*o),y=mod(_2n*e*n);return new JacobianPoint(f,h,y)}add(t){assertJacPoint(t);const{x:e,y:n,z:r}=this,{x:i,y:o,z:s}=t;if(i===_0n||o===_0n)return this;if(e===_0n||n===_0n)return t;const a=mod(r*r),c=mod(s*s),u=mod(e*c),f=mod(i*a),h=mod(mod(n*s)*c),y=mod(mod(o*r)*a),d=mod(f-u),l=mod(y-h);if(d===_0n)return l===_0n?this.double():JacobianPoint.ZERO;const m=mod(d*d),g=mod(d*m),p=mod(u*m),w=mod(l*l-g-_2n*p),S=mod(l*(p-w)-h*g),b=mod(r*s*d);return new JacobianPoint(w,S,b)}subtract(t){return this.add(t.negate())}multiplyUnsafe(t){const e=JacobianPoint.ZERO;if("bigint"==typeof t&&t===_0n)return e;let n=normalizeScalar(t);if(n===_1n)return this;if(!USE_ENDOMORPHISM){let t=e,r=this;for(;n>_0n;)n&_1n&&(t=t.add(r)),r=r.double(),n>>=_1n;return t}let{k1neg:r,k1:i,k2neg:o,k2:s}=endo.splitScalar(n),a=e,c=e,u=this;for(;i>_0n||s>_0n;)i&_1n&&(a=a.add(u)),s&_1n&&(c=c.add(u)),u=u.double(),i>>=_1n,s>>=_1n;return r&&(a=a.negate()),o&&(c=c.negate()),c=new JacobianPoint(mod(c.x*endo.beta),c.y,c.z),a.add(c)}precomputeWindow(t){const e=USE_ENDOMORPHISM?128/t+1:256/t+1,n=[];let r=this,i=r;for(let o=0;o<e;o++){i=r,n.push(i);for(let e=1;e<2**(t-1);e++)i=i.add(r),n.push(i);r=i.double()}return n}wNAF(t,e){!e&&this.equals(JacobianPoint.BASE)&&(e=Point.BASE);const n=e&&e._WINDOW_SIZE||1;if(256%n)throw new Error("Point#wNAF: Invalid precomputation window, must be power of 2");let r=e&&pointPrecomputes.get(e);r||(r=this.precomputeWindow(n),e&&1!==n&&(r=JacobianPoint.normalizeZ(r),pointPrecomputes.set(e,r)));let i=JacobianPoint.ZERO,o=JacobianPoint.BASE;const s=1+(USE_ENDOMORPHISM?128/n:256/n),a=2**(n-1),c=BigInt(2**n-1),u=2**n,f=BigInt(n);for(let e=0;e<s;e++){const n=e*a;let s=Number(t&c);t>>=f,s>a&&(s-=u,t+=_1n);const h=n,y=n+Math.abs(s)-1,d=e%2!=0,l=s<0;0===s?o=o.add(constTimeNegate(d,r[h])):i=i.add(constTimeNegate(l,r[y]))}return{p:i,f:o}}multiply(t,e){let n,r,i=normalizeScalar(t);if(USE_ENDOMORPHISM){const{k1neg:t,k1:o,k2neg:s,k2:a}=endo.splitScalar(i);let{p:c,f:u}=this.wNAF(o,e),{p:f,f:h}=this.wNAF(a,e);c=constTimeNegate(t,c),f=constTimeNegate(s,f),f=new JacobianPoint(mod(f.x*endo.beta),f.y,f.z),n=c.add(f),r=u.add(h)}else{const{p:t,f:o}=this.wNAF(i,e);n=t,r=o}return JacobianPoint.normalizeZ([n,r])[0]}toAffine(t){const{x:e,y:n,z:r}=this,i=this.equals(JacobianPoint.ZERO);null==t&&(t=i?_8n:invert(r));const o=t,s=mod(o*o),a=mod(s*o),c=mod(e*s),u=mod(n*a),f=mod(r*o);if(i)return Point.ZERO;if(f!==_1n)throw new Error("invZ was invalid");return new Point(c,u)}}function constTimeNegate(t,e){const n=e.negate();return t?n:e}JacobianPoint.BASE=new JacobianPoint(CURVE.Gx,CURVE.Gy,_1n),JacobianPoint.ZERO=new JacobianPoint(_0n,_1n,_0n);const pointPrecomputes=new WeakMap;class Point{constructor(t,e){this.x=t,this.y=e}_setWindowSize(t){this._WINDOW_SIZE=t,pointPrecomputes.delete(this)}hasEvenY(){return this.y%_2n===_0n}static fromCompressedHex(t){const e=32===t.length,n=bytesToNumber(e?t:t.subarray(1));if(!isValidFieldElement(n))throw new Error("Point is not on curve");let r=sqrtMod(weierstrass(n));const i=(r&_1n)===_1n;if(e)i&&(r=mod(-r));else{!(1&~t[0])!==i&&(r=mod(-r))}const o=new Point(n,r);return o.assertValidity(),o}static fromUncompressedHex(t){const e=bytesToNumber(t.subarray(1,33)),n=bytesToNumber(t.subarray(33,65)),r=new Point(e,n);return r.assertValidity(),r}static fromHex(t){const e=ensureBytes(t),n=e.length,r=e[0];if(32===n)return this.fromCompressedHex(e);if(33===n&&(2===r||3===r))return this.fromCompressedHex(e);if(65===n&&4===r)return this.fromUncompressedHex(e);throw new Error(`Point.fromHex: received invalid point. Expected 32-33 compressed bytes or 65 uncompressed bytes, not ${n}`)}static fromPrivateKey(t){return Point.BASE.multiply(normalizePrivateKey(t))}static fromSignature(t,e,n){const{r:r,s:i}=normalizeSignature(e);if(![0,1,2,3].includes(n))throw new Error("Cannot recover: invalid recovery bit");const o=truncateHash(ensureBytes(t)),{n:s}=CURVE,a=2===n||3===n?r+s:r,c=invert(a,s),u=mod(-o*c,s),f=mod(i*c,s),h=1&n?"03":"02",y=Point.fromHex(h+numTo32bStr(a)),d=Point.BASE.multiplyAndAddUnsafe(y,u,f);if(!d)throw new Error("Cannot recover signature: point at infinify");return d.assertValidity(),d}toRawBytes(t=!1){return hexToBytes(this.toHex(t))}toHex(t=!1){const e=numTo32bStr(this.x);if(t){return`${this.hasEvenY()?"02":"03"}${e}`}return`04${e}${numTo32bStr(this.y)}`}toHexX(){return this.toHex(!0).slice(2)}toRawX(){return this.toRawBytes(!0).slice(1)}assertValidity(){const t="Point is not on elliptic curve",{x:e,y:n}=this;if(!isValidFieldElement(e)||!isValidFieldElement(n))throw new Error(t);const r=mod(n*n);if(mod(r-weierstrass(e))!==_0n)throw new Error(t)}equals(t){return this.x===t.x&&this.y===t.y}negate(){return new Point(this.x,mod(-this.y))}double(){return JacobianPoint.fromAffine(this).double().toAffine()}add(t){return JacobianPoint.fromAffine(this).add(JacobianPoint.fromAffine(t)).toAffine()}subtract(t){return this.add(t.negate())}multiply(t){return JacobianPoint.fromAffine(this).multiply(t,this).toAffine()}multiplyAndAddUnsafe(t,e,n){const r=JacobianPoint.fromAffine(this),i=e===_0n||e===_1n||this!==Point.BASE?r.multiplyUnsafe(e):r.multiply(e),o=JacobianPoint.fromAffine(t).multiplyUnsafe(n),s=i.add(o);return s.equals(JacobianPoint.ZERO)?void 0:s.toAffine()}}function sliceDER(t){return Number.parseInt(t[0],16)>=8?"00"+t:t}function parseDERInt(t){if(t.length<2||2!==t[0])throw new Error(`Invalid signature integer tag: ${bytesToHex(t)}`);const e=t[1],n=t.subarray(2,e+2);if(!e||n.length!==e)throw new Error("Invalid signature integer: wrong length");if(0===n[0]&&n[1]<=127)throw new Error("Invalid signature integer: trailing length");return{data:bytesToNumber(n),left:t.subarray(e+2)}}function parseDERSignature(t){if(t.length<2||48!=t[0])throw new Error(`Invalid signature tag: ${bytesToHex(t)}`);if(t[1]!==t.length-2)throw new Error("Invalid signature: incorrect length");const{data:e,left:n}=parseDERInt(t.subarray(2)),{data:r,left:i}=parseDERInt(n);if(i.length)throw new Error(`Invalid signature: left bytes after parsing: ${bytesToHex(i)}`);return{r:e,s:r}}exports.Point=Point,Point.BASE=new Point(CURVE.Gx,CURVE.Gy),Point.ZERO=new Point(_0n,_0n);class Signature{constructor(t,e){this.r=t,this.s=e,this.assertValidity()}static fromCompact(t){const e=t instanceof Uint8Array,n="Signature.fromCompact";if("string"!=typeof t&&!e)throw new TypeError(`${n}: Expected string or Uint8Array`);const r=e?bytesToHex(t):t;if(128!==r.length)throw new Error(`${n}: Expected 64-byte hex`);return new Signature(hexToNumber(r.slice(0,64)),hexToNumber(r.slice(64,128)))}static fromDER(t){const e=t instanceof Uint8Array;if("string"!=typeof t&&!e)throw new TypeError("Signature.fromDER: Expected string or Uint8Array");const{r:n,s:r}=parseDERSignature(e?t:hexToBytes(t));return new Signature(n,r)}static fromHex(t){return this.fromDER(t)}assertValidity(){const{r:t,s:e}=this;if(!isWithinCurveOrder(t))throw new Error("Invalid Signature: r must be 0 < r < n");if(!isWithinCurveOrder(e))throw new Error("Invalid Signature: s must be 0 < s < n")}hasHighS(){const t=CURVE.n>>_1n;return this.s>t}normalizeS(){return this.hasHighS()?new Signature(this.r,mod(-this.s,CURVE.n)):this}toDERRawBytes(){return hexToBytes(this.toDERHex())}toDERHex(){const t=sliceDER(numberToHexUnpadded(this.s)),e=sliceDER(numberToHexUnpadded(this.r)),n=t.length/2,r=e.length/2,i=numberToHexUnpadded(n),o=numberToHexUnpadded(r);return`30${numberToHexUnpadded(r+n+4)}02${o}${e}02${i}${t}`}toRawBytes(){return this.toDERRawBytes()}toHex(){return this.toDERHex()}toCompactRawBytes(){return hexToBytes(this.toCompactHex())}toCompactHex(){return numTo32bStr(this.r)+numTo32bStr(this.s)}}function concatBytes(...t){if(!t.every((t=>t instanceof Uint8Array)))throw new Error("Uint8Array list expected");if(1===t.length)return t[0];const e=t.reduce(((t,e)=>t+e.length),0),n=new Uint8Array(e);for(let e=0,r=0;e<t.length;e++){const i=t[e];n.set(i,r),r+=i.length}return n}exports.Signature=Signature;const hexes=Array.from({length:256},((t,e)=>e.toString(16).padStart(2,"0")));function bytesToHex(t){if(!(t instanceof Uint8Array))throw new Error("Expected Uint8Array");let e="";for(let n=0;n<t.length;n++)e+=hexes[t[n]];return e}const POW_2_256=BigInt("0x10000000000000000000000000000000000000000000000000000000000000000");function numTo32bStr(t){if("bigint"!=typeof t)throw new Error("Expected bigint");if(!(_0n<=t&&t<POW_2_256))throw new Error("Expected number 0 <= n < 2^256");return t.toString(16).padStart(64,"0")}function numTo32b(t){const e=hexToBytes(numTo32bStr(t));if(32!==e.length)throw new Error("Error: expected 32 bytes");return e}function numberToHexUnpadded(t){const e=t.toString(16);return 1&e.length?`0${e}`:e}function hexToNumber(t){if("string"!=typeof t)throw new TypeError("hexToNumber: expected string, got "+typeof t);return BigInt(`0x${t}`)}function hexToBytes(t){if("string"!=typeof t)throw new TypeError("hexToBytes: expected string, got "+typeof t);if(t.length%2)throw new Error("hexToBytes: received invalid unpadded hex"+t.length);const e=new Uint8Array(t.length/2);for(let n=0;n<e.length;n++){const r=2*n,i=t.slice(r,r+2),o=Number.parseInt(i,16);if(Number.isNaN(o)||o<0)throw new Error("Invalid byte sequence");e[n]=o}return e}function bytesToNumber(t){return hexToNumber(bytesToHex(t))}function ensureBytes(t){return t instanceof Uint8Array?Uint8Array.from(t):hexToBytes(t)}function normalizeScalar(t){if("number"==typeof t&&Number.isSafeInteger(t)&&t>0)return BigInt(t);if("bigint"==typeof t&&isWithinCurveOrder(t))return t;throw new TypeError("Expected valid private scalar: 0 < scalar < curve.n")}function mod(t,e=CURVE.P){const n=t%e;return n>=_0n?n:e+n}function pow2(t,e){const{P:n}=CURVE;let r=t;for(;e-- >_0n;)r*=r,r%=n;return r}function sqrtMod(t){const{P:e}=CURVE,n=BigInt(6),r=BigInt(11),i=BigInt(22),o=BigInt(23),s=BigInt(44),a=BigInt(88),c=t*t*t%e,u=c*c*t%e,f=pow2(u,_3n)*u%e,h=pow2(f,_3n)*u%e,y=pow2(h,_2n)*c%e,d=pow2(y,r)*y%e,l=pow2(d,i)*d%e,m=pow2(l,s)*l%e,g=pow2(m,a)*m%e,p=pow2(g,s)*l%e,w=pow2(p,_3n)*u%e,S=pow2(w,o)*d%e,b=pow2(S,n)*c%e,E=pow2(b,_2n);if(E*E%e!==t)throw new Error("Cannot find square root");return E}function invert(t,e=CURVE.P){if(t===_0n||e<=_0n)throw new Error(`invert: expected positive integers, got n=${t} mod=${e}`);let n=mod(t,e),r=e,i=_0n,o=_1n,s=_1n,a=_0n;for(;n!==_0n;){const t=r/n,e=r%n,c=i-s*t,u=o-a*t;r=n,n=e,i=s,o=a,s=c,a=u}if(r!==_1n)throw new Error("invert: does not exist");return mod(i,e)}function invertBatch(t,e=CURVE.P){const n=new Array(t.length),r=invert(t.reduce(((t,r,i)=>r===_0n?t:(n[i]=t,mod(t*r,e))),_1n),e);return t.reduceRight(((t,r,i)=>r===_0n?t:(n[i]=mod(t*n[i],e),mod(t*r,e))),r),n}function bits2int_2(t){const e=8*t.length-256,n=bytesToNumber(t);return e>0?n>>BigInt(e):n}function truncateHash(t,e=!1){const n=bits2int_2(t);if(e)return n;const{n:r}=CURVE;return n>=r?n-r:n}let _sha256Sync,_hmacSha256Sync;class HmacDrbg{constructor(t,e){if(this.hashLen=t,this.qByteLen=e,"number"!=typeof t||t<2)throw new Error("hashLen must be a number");if("number"!=typeof e||e<2)throw new Error("qByteLen must be a number");this.v=new Uint8Array(t).fill(1),this.k=new Uint8Array(t).fill(0),this.counter=0}hmac(...t){return exports.utils.hmacSha256(this.k,...t)}hmacSync(...t){return _hmacSha256Sync(this.k,...t)}checkSync(){if("function"!=typeof _hmacSha256Sync)throw new ShaError("hmacSha256Sync needs to be set")}incr(){if(this.counter>=1e3)throw new Error("Tried 1,000 k values for sign(), all were invalid");this.counter+=1}async reseed(t=new Uint8Array){this.k=await this.hmac(this.v,Uint8Array.from([0]),t),this.v=await this.hmac(this.v),0!==t.length&&(this.k=await this.hmac(this.v,Uint8Array.from([1]),t),this.v=await this.hmac(this.v))}reseedSync(t=new Uint8Array){this.checkSync(),this.k=this.hmacSync(this.v,Uint8Array.from([0]),t),this.v=this.hmacSync(this.v),0!==t.length&&(this.k=this.hmacSync(this.v,Uint8Array.from([1]),t),this.v=this.hmacSync(this.v))}async generate(){this.incr();let t=0;const e=[];for(;t<this.qByteLen;){this.v=await this.hmac(this.v);const n=this.v.slice();e.push(n),t+=this.v.length}return concatBytes(...e)}generateSync(){this.checkSync(),this.incr();let t=0;const e=[];for(;t<this.qByteLen;){this.v=this.hmacSync(this.v);const n=this.v.slice();e.push(n),t+=this.v.length}return concatBytes(...e)}}function isWithinCurveOrder(t){return _0n<t&&t<CURVE.n}function isValidFieldElement(t){return _0n<t&&t<CURVE.P}function kmdToSig(t,e,n,r=!0){const{n:i}=CURVE,o=truncateHash(t,!0);if(!isWithinCurveOrder(o))return;const s=invert(o,i),a=Point.BASE.multiply(o),c=mod(a.x,i);if(c===_0n)return;const u=mod(s*mod(e+n*c,i),i);if(u===_0n)return;let f=new Signature(c,u),h=(a.x===f.r?0:2)|Number(a.y&_1n);return r&&f.hasHighS()&&(f=f.normalizeS(),h^=1),{sig:f,recovery:h}}function normalizePrivateKey(t){let e;if("bigint"==typeof t)e=t;else if("number"==typeof t&&Number.isSafeInteger(t)&&t>0)e=BigInt(t);else if("string"==typeof t){if(64!==t.length)throw new Error("Expected 32 bytes of private key");e=hexToNumber(t)}else{if(!(t instanceof Uint8Array))throw new TypeError("Expected valid private key");if(32!==t.length)throw new Error("Expected 32 bytes of private key");e=bytesToNumber(t)}if(!isWithinCurveOrder(e))throw new Error("Expected private key: 0 < key < n");return e}function normalizePublicKey(t){return t instanceof Point?(t.assertValidity(),t):Point.fromHex(t)}function normalizeSignature(t){if(t instanceof Signature)return t.assertValidity(),t;try{return Signature.fromDER(t)}catch(e){return Signature.fromCompact(t)}}function getPublicKey(t,e=!1){return Point.fromPrivateKey(t).toRawBytes(e)}function recoverPublicKey(t,e,n,r=!1){return Point.fromSignature(t,e,n).toRawBytes(r)}function isProbPub(t){const e=t instanceof Uint8Array,n="string"==typeof t,r=(e||n)&&t.length;return e?33===r||65===r:n?66===r||130===r:t instanceof Point}function getSharedSecret(t,e,n=!1){if(isProbPub(t))throw new TypeError("getSharedSecret: first arg must be private key");if(!isProbPub(e))throw new TypeError("getSharedSecret: second arg must be public key");const r=normalizePublicKey(e);return r.assertValidity(),r.multiply(normalizePrivateKey(t)).toRawBytes(n)}function bits2int(t){return bytesToNumber(t.length>32?t.slice(0,32):t)}function bits2octets(t){const e=bits2int(t),n=mod(e,CURVE.n);return int2octets(n<_0n?e:n)}function int2octets(t){return numTo32b(t)}function initSigArgs(t,e,n){if(null==t)throw new Error(`sign: expected valid message hash, not "${t}"`);const r=ensureBytes(t),i=normalizePrivateKey(e),o=[int2octets(i),bits2octets(r)];if(null!=n){!0===n&&(n=exports.utils.randomBytes(32));const t=ensureBytes(n);if(32!==t.length)throw new Error("sign: Expected 32 bytes of extra data");o.push(t)}return{seed:concatBytes(...o),m:bits2int(r),d:i}}function finalizeSig(t,e){const{sig:n,recovery:r}=t,{der:i,recovered:o}=Object.assign({canonical:!0,der:!0},e),s=i?n.toDERRawBytes():n.toCompactRawBytes();return o?[s,r]:s}async function sign(t,e,n={}){const{seed:r,m:i,d:o}=initSigArgs(t,e,n.extraEntropy),s=new HmacDrbg(32,32);let a;for(await s.reseed(r);!(a=kmdToSig(await s.generate(),i,o,n.canonical));)await s.reseed();return finalizeSig(a,n)}function signSync(t,e,n={}){const{seed:r,m:i,d:o}=initSigArgs(t,e,n.extraEntropy),s=new HmacDrbg(32,32);let a;for(s.reseedSync(r);!(a=kmdToSig(s.generateSync(),i,o,n.canonical));)s.reseedSync();return finalizeSig(a,n)}exports.getPublicKey=getPublicKey,exports.recoverPublicKey=recoverPublicKey,exports.getSharedSecret=getSharedSecret,exports.sign=sign,exports.signSync=signSync;const vopts={strict:!0};function verify(t,e,n,r=vopts){let i;try{i=normalizeSignature(t),e=ensureBytes(e)}catch(t){return!1}const{r:o,s:s}=i;if(r.strict&&i.hasHighS())return!1;const a=truncateHash(e);let c;try{c=normalizePublicKey(n)}catch(t){return!1}const{n:u}=CURVE,f=invert(s,u),h=mod(a*f,u),y=mod(o*f,u),d=Point.BASE.multiplyAndAddUnsafe(c,h,y);if(!d)return!1;return mod(d.x,u)===o}function schnorrChallengeFinalize(t){return mod(bytesToNumber(t),CURVE.n)}exports.verify=verify;class SchnorrSignature{constructor(t,e){this.r=t,this.s=e,this.assertValidity()}static fromHex(t){const e=ensureBytes(t);if(64!==e.length)throw new TypeError(`SchnorrSignature.fromHex: expected 64 bytes, not ${e.length}`);const n=bytesToNumber(e.subarray(0,32)),r=bytesToNumber(e.subarray(32,64));return new SchnorrSignature(n,r)}assertValidity(){const{r:t,s:e}=this;if(!isValidFieldElement(t)||!isWithinCurveOrder(e))throw new Error("Invalid signature")}toHex(){return numTo32bStr(this.r)+numTo32bStr(this.s)}toRawBytes(){return hexToBytes(this.toHex())}}function schnorrGetPublicKey(t){return Point.fromPrivateKey(t).toRawX()}class InternalSchnorrSignature{constructor(t,e,n=exports.utils.randomBytes()){if(null==t)throw new TypeError(`sign: Expected valid message, not "${t}"`);this.m=ensureBytes(t);const{x:r,scalar:i}=this.getScalar(normalizePrivateKey(e));if(this.px=r,this.d=i,this.rand=ensureBytes(n),32!==this.rand.length)throw new TypeError("sign: Expected 32 bytes of aux randomness")}getScalar(t){const e=Point.fromPrivateKey(t),n=e.hasEvenY()?t:CURVE.n-t;return{point:e,scalar:n,x:e.toRawX()}}initNonce(t,e){return numTo32b(t^bytesToNumber(e))}finalizeNonce(t){const e=mod(bytesToNumber(t),CURVE.n);if(e===_0n)throw new Error("sign: Creation of signature failed. k is zero");const{point:n,x:r,scalar:i}=this.getScalar(e);return{R:n,rx:r,k:i}}finalizeSig(t,e,n,r){return new SchnorrSignature(t.x,mod(e+n*r,CURVE.n)).toRawBytes()}error(){throw new Error("sign: Invalid signature produced")}async calc(){const{m:t,d:e,px:n,rand:r}=this,i=exports.utils.taggedHash,o=this.initNonce(e,await i(TAGS.aux,r)),{R:s,rx:a,k:c}=this.finalizeNonce(await i(TAGS.nonce,o,n,t)),u=schnorrChallengeFinalize(await i(TAGS.challenge,a,n,t)),f=this.finalizeSig(s,c,u,e);return await schnorrVerify(f,t,n)||this.error(),f}calcSync(){const{m:t,d:e,px:n,rand:r}=this,i=exports.utils.taggedHashSync,o=this.initNonce(e,i(TAGS.aux,r)),{R:s,rx:a,k:c}=this.finalizeNonce(i(TAGS.nonce,o,n,t)),u=schnorrChallengeFinalize(i(TAGS.challenge,a,n,t)),f=this.finalizeSig(s,c,u,e);return schnorrVerifySync(f,t,n)||this.error(),f}}async function schnorrSign(t,e,n){return new InternalSchnorrSignature(t,e,n).calc()}function schnorrSignSync(t,e,n){return new InternalSchnorrSignature(t,e,n).calcSync()}function initSchnorrVerify(t,e,n){const r=t instanceof SchnorrSignature,i=r?t:SchnorrSignature.fromHex(t);return r&&i.assertValidity(),{...i,m:ensureBytes(e),P:normalizePublicKey(n)}}function finalizeSchnorrVerify(t,e,n,r){const i=Point.BASE.multiplyAndAddUnsafe(e,normalizePrivateKey(n),mod(-r,CURVE.n));return!(!i||!i.hasEvenY()||i.x!==t)}async function schnorrVerify(t,e,n){try{const{r:r,s:i,m:o,P:s}=initSchnorrVerify(t,e,n),a=schnorrChallengeFinalize(await exports.utils.taggedHash(TAGS.challenge,numTo32b(r),s.toRawX(),o));return finalizeSchnorrVerify(r,s,i,a)}catch(t){return!1}}function schnorrVerifySync(t,e,n){try{const{r:r,s:i,m:o,P:s}=initSchnorrVerify(t,e,n),a=schnorrChallengeFinalize(exports.utils.taggedHashSync(TAGS.challenge,numTo32b(r),s.toRawX(),o));return finalizeSchnorrVerify(r,s,i,a)}catch(t){if(t instanceof ShaError)throw t;return!1}}exports.schnorr={Signature:SchnorrSignature,getPublicKey:schnorrGetPublicKey,sign:schnorrSign,verify:schnorrVerify,signSync:schnorrSignSync,verifySync:schnorrVerifySync},Point.BASE._setWindowSize(8);const crypto={node:nodeCrypto,web:"object"==typeof self&&"crypto"in self?self.crypto:void 0},TAGS={challenge:"BIP0340/challenge",aux:"BIP0340/aux",nonce:"BIP0340/nonce"},TAGGED_HASH_PREFIXES={};exports.utils={bytesToHex:bytesToHex,hexToBytes:hexToBytes,concatBytes:concatBytes,mod:mod,invert:invert,isValidPrivateKey(t){try{return normalizePrivateKey(t),!0}catch(t){return!1}},_bigintTo32Bytes:numTo32b,_normalizePrivateKey:normalizePrivateKey,hashToPrivateKey:t=>{if((t=ensureBytes(t)).length<40||t.length>1024)throw new Error("Expected valid bytes of private key as per FIPS 186");return numTo32b(mod(bytesToNumber(t),CURVE.n-_1n)+_1n)},randomBytes:(t=32)=>{if(crypto.web)return crypto.web.getRandomValues(new Uint8Array(t));if(crypto.node){const{randomBytes:e}=crypto.node;return Uint8Array.from(e(t))}throw new Error("The environment doesn't have randomBytes function")},randomPrivateKey:()=>exports.utils.hashToPrivateKey(exports.utils.randomBytes(40)),precompute(t=8,e=Point.BASE){const n=e===Point.BASE?e:new Point(e.x,e.y);return n._setWindowSize(t),n.multiply(_3n),n},sha256:async(...t)=>{if(crypto.web){const e=await crypto.web.subtle.digest("SHA-256",concatBytes(...t));return new Uint8Array(e)}if(crypto.node){const{createHash:e}=crypto.node,n=e("sha256");return t.forEach((t=>n.update(t))),Uint8Array.from(n.digest())}throw new Error("The environment doesn't have sha256 function")},hmacSha256:async(t,...e)=>{if(crypto.web){const n=await crypto.web.subtle.importKey("raw",t,{name:"HMAC",hash:{name:"SHA-256"}},!1,["sign"]),r=concatBytes(...e),i=await crypto.web.subtle.sign("HMAC",n,r);return new Uint8Array(i)}if(crypto.node){const{createHmac:n}=crypto.node,r=n("sha256",t);return e.forEach((t=>r.update(t))),Uint8Array.from(r.digest())}throw new Error("The environment doesn't have hmac-sha256 function")},sha256Sync:void 0,hmacSha256Sync:void 0,taggedHash:async(t,...e)=>{let n=TAGGED_HASH_PREFIXES[t];if(void 0===n){const e=await exports.utils.sha256(Uint8Array.from(t,(t=>t.charCodeAt(0))));n=concatBytes(e,e),TAGGED_HASH_PREFIXES[t]=n}return exports.utils.sha256(n,...e)},taggedHashSync:(t,...e)=>{if("function"!=typeof _sha256Sync)throw new ShaError("sha256Sync is undefined, you need to set it");let n=TAGGED_HASH_PREFIXES[t];if(void 0===n){const e=_sha256Sync(Uint8Array.from(t,(t=>t.charCodeAt(0))));n=concatBytes(e,e),TAGGED_HASH_PREFIXES[t]=n}return _sha256Sync(n,...e)},_JacobianPoint:JacobianPoint},Object.defineProperties(exports.utils,{sha256Sync:{configurable:!1,get:()=>_sha256Sync,set(t){_sha256Sync||(_sha256Sync=t)}},hmacSha256Sync:{configurable:!1,get:()=>_hmacSha256Sync,set(t){_hmacSha256Sync||(_hmacSha256Sync=t)}}});
    return module.exports;
  })();
  function yieldUi() {
    return new Promise(function (r) { setTimeout(r, 0); });
  }
  function setLiveBusy(busy) {
    var root = document.getElementById("entropy-desk-root");
    if (!root) return;
    var btns = root.querySelectorAll(".ed-liveboth, .ed-buy, .ed-sell, .ed-cancel, .ed-cancelall");
    for (var i = 0; i < btns.length; i++) {
      if (busy) btns[i].setAttribute("disabled", "disabled");
      else btns[i].removeAttribute("disabled");
    }
  }
  function utf8Bytes(s) {
    return new TextEncoder().encode(String(s));
  }
  function bytesToHexPlain(u8) {
    var s = "";
    for (var i = 0; i < u8.length; i++) s += (u8[i] + 256).toString(16).slice(-2);
    return s;
  }
  function toChecksumAddress(hex40) {
    var lower = String(hex40).replace(/^0x/i, "").toLowerCase();
    var hash = bytesToHexPlain(keccak256(utf8Bytes(lower)));
    var out = "0x";
    for (var i = 0; i < 40; i++) out += parseInt(hash[i], 16) >= 8 ? lower[i].toUpperCase() : lower[i];
    return out;
  }
  async function fetchJson(url, body, timeoutMs, errPrefix) {
    async function once() {
      var ac = new AbortController();
      var ms = timeoutMs || 8000;
      var t = setTimeout(function () { try { ac.abort(); } catch (e0) {} }, ms);
      try {
        var res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: ac.signal
        });
        var json = await res.json().catch(function () { return null; });
        return { res: res, json: json };
      } catch (e1) {
        if (e1 && (e1.name === "AbortError" || /aborted|AbortError/i.test(String(e1 && e1.message || e1)))) {
          throw new Error((errPrefix || "请求") + " 超时");
        }
        throw e1;
      } finally {
        clearTimeout(t);
      }
    }
    var out = await once();
    if (out.res.status === 429) {
      await sleepMs(700);
      out = await once();
    }
    if (out.res.status === 429 && url === INFO_URL) return null;
    if (!out.res.ok) throw new Error((errPrefix || "HTTP") + " " + out.res.status);
    return out.json;
  }
  async function hlInfo(body) {
    return fetchJson(INFO_URL, body, 8000, "info HTTP");
  }
  async function resolveAssets() {
    var dexs = await hlInfo({ type: "perpDexs" });
    if (!Array.isArray(dexs)) throw new Error("perpDexs 无效");
    var dexIndex = -1, i;
    for (i = 0; i < dexs.length; i++) {
      var d = dexs[i];
      if (!d) continue;
      var name = typeof d === "string" ? d : d.name;
      if (name === DEX) { dexIndex = i; break; }
    }
    if (dexIndex < 0) throw new Error("未找到 DEX io");
    var pair = await hlInfo({ type: "metaAndAssetCtxs", dex: DEX });
    if (!Array.isArray(pair) || !pair[0] || !pair[0].universe) throw new Error("metaAndAssetCtxs dex=io 无效");
    var uni = pair[0].universe;
    var ctxs = pair[1] || [];
    var assets = {};
    for (i = 0; i < uni.length; i++) {
      var rawName = String(uni[i].name || "");
      var coin = rawName.indexOf(":") === -1 ? DEX + ":" + rawName : rawName;
      if (/^(xyz|vntl):/i.test(coin)) continue;
      if (ALLOWED.indexOf(coin) === -1) continue;
      assets[coin] = { coin: coin, indexInMeta: i, assetId: 100000 + dexIndex * 10000 + i, szDecimals: uni[i].szDecimals | 0, maxLeverage: uni[i].maxLeverage, ctx: ctxs[i] || {} };
    }
    if (!assets["io:ANTH"] || !assets["io:SNDK"]) throw new Error("io universe 缺少 ANTH/SNDK");
    state.dexIndex = dexIndex;
    state.assets = assets;
    log("资产解析 OK  dex=io  perp_dex_index=" + dexIndex + "  ANTH=" + assets["io:ANTH"].assetId + "  SNDK=" + assets["io:SNDK"].assetId);
    return assets;
  }
  function applyCtxs(pair) {
    if (!pair || !pair[0] || !pair[0].universe) return;
    var uni = pair[0].universe;
    var ctxs = pair[1] || [];
    for (var i = 0; i < uni.length; i++) {
      var rawName = String(uni[i].name || "");
      var coin = rawName.indexOf(":") === -1 ? DEX + ":" + rawName : rawName;
      if (!state.assets[coin]) continue;
      state.assets[coin].ctx = ctxs[i] || {};
      state.assets[coin].szDecimals = uni[i].szDecimals | 0;
    }
  }
  async function fetchBook(coin) {
    assertAllowedCoin(coin);
    try {
      var book = await hlInfo({ type: "l2Book", coin: coin, nSigFigs: 5 });
      var bids = (book && book.levels && book.levels[0]) || [];
      var asks = (book && book.levels && book.levels[1]) || [];
      var bidPx = bids[0] && bids[0].px != null ? String(bids[0].px) : "";
      var askPx = asks[0] && asks[0].px != null ? String(asks[0].px) : "";
      var bid = bidPx ? Number(bidPx) : NaN;
      var ask = askPx ? Number(askPx) : NaN;
      var mid = Number.isFinite(bid) && Number.isFinite(ask) ? (bid + ask) / 2 : NaN;
      return { bid: bid, ask: ask, bidPx: bidPx, askPx: askPx, mid: mid, bids: bids, asks: asks, src: "l2Book" };
    } catch (e1) {
      warn(coin, "l2Book 失败，回退 midPx", e1 && e1.message);
      return { bid: NaN, ask: NaN, bidPx: "", askPx: "", mid: NaN, bids: [], asks: [], src: "fail" };
    }
  }
  function mergeQuote(coin, book) {
    var a = state.assets[coin];
    var ctx = (a && a.ctx) || {};
    var midPx = Number(ctx.midPx);
    var mark = Number(ctx.markPx);
    var bid = book.bid, ask = book.ask, mid = book.mid;
    var bidPx = book.bidPx != null && book.bidPx !== "" ? String(book.bidPx) : "";
    var askPx = book.askPx != null && book.askPx !== "" ? String(book.askPx) : "";
    if (!Number.isFinite(mid) && Number.isFinite(midPx)) { mid = midPx; book.src = (book.src || "") + "+midPx"; }
    if (!Number.isFinite(bid) && Number.isFinite(mid)) bid = mid;
    if (!Number.isFinite(ask) && Number.isFinite(mid)) ask = mid;
    var spread = Number.isFinite(bid) && Number.isFinite(ask) ? ask - bid : NaN;
    var q = { coin: coin, bid: bid, ask: ask, bidPx: bidPx, askPx: askPx, mid: mid, spread: spread, mark: mark, vol24h: Number(ctx.dayNtlVlm), src: book.src, ts: nowMs(), assetId: a ? a.assetId : null, szDecimals: a ? a.szDecimals : 0 };
    state.quotes[coin] = q;
    return q;
  }
  function aloPrices(q) {
    var szd = q.szDecimals | 0;
    var bidPx = q.bidPx != null && q.bidPx !== "" ? String(q.bidPx) : "";
    var askPx = q.askPx != null && q.askPx !== "" ? String(q.askPx) : "";
    if (bidPx && askPx && Number(bidPx) < Number(askPx)) {
      return { buy: bidPx, sell: askPx };
    }
    var mid = q.mid;
    if (!Number.isFinite(mid) || mid <= 0) mid = q.mark;
    if (!Number.isFinite(mid) || mid <= 0) {
      var a0 = state.assets[q.coin] || {};
      var ctx0 = a0.ctx || {};
      mid = Number(ctx0.markPx);
      if (!Number.isFinite(mid) || mid <= 0) mid = Number(ctx0.midPx);
    }
    if (!Number.isFinite(mid) || mid <= 0) throw new Error(q.coin + " 无可用中间价");
    var tick = tickSize(mid, szd);
    var buyN = mid - tick;
    var sellN = mid + tick;
    if (!(buyN > 0)) buyN = tick;
    return { buy: roundPx(buyN, szd), sell: roundPx(sellN, szd) };
  }
  function wireMin(a, b) {
    return Number(a) <= Number(b) ? String(a) : String(b);
  }
  function wireMax(a, b) {
    return Number(a) >= Number(b) ? String(a) : String(b);
  }
  function clampMakerPx(q, desired) {
    var bid = Number(q && q.bid);
    var ask = Number(q && q.ask);
    var mid = Number(q && q.mid);
    if (Number.isFinite(bid) && Number.isFinite(ask) && bid >= ask) return null;
    var szd = (q && q.szDecimals) | 0;
    var tickSrc = Number.isFinite(mid) && mid > 0 ? mid : bid;
    if (!(Number.isFinite(tickSrc) && tickSrc > 0)) tickSrc = ask;
    if (!(Number.isFinite(tickSrc) && tickSrc > 0)) return null;
    var tick = tickSize(tickSrc, szd);
    var buy = desired && desired.buy != null && desired.buy !== "" ? String(desired.buy) : "";
    var sell = desired && desired.sell != null && desired.sell !== "" ? String(desired.sell) : "";
    if (Number.isFinite(ask) && ask > 0) {
      var cap = roundPx(Number(ask) - tick, szd);
      buy = buy ? wireMin(buy, cap) : cap;
    }
    if (Number.isFinite(bid) && bid > 0) {
      var floor = roundPx(Number(bid) + tick, szd);
      sell = sell ? wireMax(sell, floor) : floor;
    }
    if (!buy || !sell) return null;
    if (!(Number(buy) > 0) || !(Number(sell) > 0)) return null;
    if (Number(buy) >= Number(sell)) return null;
    return { buy: buy, sell: sell };
  }
  function wantedPx(q) {
    return clampMakerPx(q, aloPrices(q));
  }
  function sizeFromNotional(px, szDecimals, notional) {
    var ntl = Number(notional);
    if (!(ntl > 0)) throw new Error("名义金额必须 > 0");
    var p = Number(px);
    if (!(p > 0)) throw new Error("价格无效");
    return roundSz(ntl / p, szDecimals);
  }
  function buildOrderAction(orders) {
    var wires = [];
    for (var i = 0; i < orders.length; i++) {
      var o = orders[i];
      wires.push({ a: o.a | 0, b: !!o.b, p: String(o.p), s: String(o.s), r: !!o.r, t: { limit: { tif: "Alo" } } });
    }
    return { type: "order", orders: wires, grouping: "na", builder: { b: BUILDER_LC, f: 0 } };
  }
  function pushPaper(entry) {
    entry.ts = nowMs();
    state.paperLog.unshift(entry);
    if (state.paperLog.length > 40) state.paperLog.length = 40;
    log("纸面 " + JSON.stringify(entry));
    setStatus("纸面 " + entry.coin + " " + (entry.side === "B" ? "买" : "卖") + " " + entry.s + " @ " + entry.p, false);
  }
  async function ensureQuote(coin) {
    coin = assertAllowedCoin(coin);
    var q = state.quotes[coin];
    var fresh = q && q.ts && (nowMs() - q.ts) < 1000 && ((q.bidPx && q.askPx) || (Number.isFinite(q.mid) && q.mid > 0));
    if (fresh) return q;
    var need = !q || !Number.isFinite(q.mid) || q.mid <= 0 || !q.ts || (nowMs() - q.ts) >= 1000;
    if (need) {
      if (!state.assets[coin]) await resolveAssets();
      q = mergeQuote(coin, await fetchBook(coin));
    }
    if (!q || !Number.isFinite(q.mid) || q.mid <= 0) {
      var a = state.assets[coin] || {};
      var ctx = a.ctx || {};
      var midPx = Number(ctx.midPx);
      var markPx = Number(ctx.markPx);
      var fb = Number.isFinite(midPx) && midPx > 0 ? midPx : markPx;
      if (Number.isFinite(fb) && fb > 0) {
        q = mergeQuote(coin, { bid: fb, ask: fb, bidPx: "", askPx: "", mid: fb, bids: [], asks: [], src: "ctx" });
      }
    }
    return q;
  }
  async function paperBoth(coin) {
    coin = assertAllowedCoin(coin);
    var q = await ensureQuote(coin);
    if (!q) throw new Error("尚无行情: " + coin);
    var px = aloPrices(q);
    var buySz = sizeFromNotional(px.buy, q.szDecimals, state.notional);
    var sellSz = sizeFromNotional(px.sell, q.szDecimals, state.notional);
    pushPaper({ mode: "paper", coin: coin, side: "B", p: px.buy, s: buySz, ntl: state.notional, assetId: q.assetId });
    pushPaper({ mode: "paper", coin: coin, side: "S", p: px.sell, s: sellSz, ntl: state.notional, assetId: q.assetId });
    setStatus("纸面 " + coin + " 买 " + buySz + " @ " + px.buy + "  /  卖 " + sellSz + " @ " + px.sell, false);
    updatePaperLog();
    return { buy: px.buy, sell: px.sell, buySz: buySz, sellSz: sellSz };
  }
  function splitSig(hex) {
    var h = String(hex);
    if (h.slice(0, 2) !== "0x") h = "0x" + h;
    if (h.length < 132) throw new Error("签名长度不足");
    var r = h.slice(0, 66);
    var s = "0x" + h.slice(66, 130);
    var v = parseInt(h.slice(130, 132), 16);
    if (v < 27) v += 27;
    return { r: r, s: s, v: v };
  }
  function idbGetMany(dbName, storeName, keys) {
    return new Promise(function (resolve, reject) {
      var done = false;
      var dbRef = null;
      var timer = setTimeout(function () {
        finish(function () { reject(new Error("indexedDB 超时")); });
      }, 2000);
      function finish(fn) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        try { if (dbRef) dbRef.close(); } catch (e0) {}
        fn();
      }
      var req;
      try { req = indexedDB.open(dbName); }
      catch (eOpen) {
        finish(function () { reject(eOpen); });
        return;
      }
      req.onblocked = function () {
        finish(function () { reject(new Error("indexedDB blocked")); });
      };
      req.onupgradeneeded = function (ev) {
        try { if (ev && ev.target && ev.target.transaction) ev.target.transaction.abort(); } catch (e1) {}
        finish(function () { reject(new Error("indexedDB 拒绝升级")); });
      };
      req.onerror = function () {
        finish(function () { reject(req.error || new Error("indexedDB.open 失败")); });
      };
      req.onsuccess = function () {
        dbRef = req.result;
        try {
          var tx = dbRef.transaction(storeName, "readonly");
          var store = tx.objectStore(storeName);
          var out = new Array(keys.length);
          var pending = keys.length;
          if (!pending) { finish(function () { resolve(out); }); return; }
          keys.forEach(function (key, idx) {
            var g = store.get(key);
            g.onsuccess = function () {
              out[idx] = g.result;
              pending -= 1;
              if (pending === 0) finish(function () { resolve(out); });
            };
            g.onerror = function () {
              finish(function () { reject(g.error); });
            };
          });
        } catch (e2) {
          finish(function () { reject(e2); });
        }
      };
    });
  }
  function idbGet(dbName, storeName, key) {
    return new Promise(function (resolve, reject) {
      var done = false;
      var dbRef = null;
      var timer = setTimeout(function () {
        finish(function () { reject(new Error("indexedDB 超时")); });
      }, 2000);
      function finish(fn) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        try { if (dbRef) dbRef.close(); } catch (e0) {}
        fn();
      }
      var req;
      try { req = indexedDB.open(dbName); }
      catch (eOpen) {
        finish(function () { reject(eOpen); });
        return;
      }
      req.onblocked = function () {
        finish(function () { reject(new Error("indexedDB blocked")); });
      };
      req.onupgradeneeded = function (ev) {
        try { if (ev && ev.target && ev.target.transaction) ev.target.transaction.abort(); } catch (e1) {}
        finish(function () { reject(new Error("indexedDB 拒绝升级")); });
      };
      req.onerror = function () {
        finish(function () { reject(req.error || new Error("indexedDB.open 失败")); });
      };
      req.onsuccess = function () {
        dbRef = req.result;
        try {
          var tx = dbRef.transaction(storeName, "readonly");
          var g = tx.objectStore(storeName).get(key);
          g.onsuccess = function () {
            var val = g.result;
            finish(function () { resolve(val); });
          };
          g.onerror = function () {
            finish(function () { reject(g.error); });
          };
        } catch (e2) {
          finish(function () { reject(e2); });
        }
      };
    });
  }
  var AGENT_NEED_SITE = "请先在 entropy 交易页自己下一笔任意单以批准 agent";
  function isAsciiHexKeyBytes(u8) {
    if (u8.length !== 64 && u8.length !== 66) return false;
    for (var i = 0; i < u8.length; i++) {
      var c = u8[i];
      var hex = (c >= 48 && c <= 57) || (c >= 97 && c <= 102) || (c >= 65 && c <= 70);
      if (i < 2 && u8.length === 66) {
        if (c === 48 && i === 0) continue;
        if ((c === 120 || c === 88) && i === 1) continue;
      }
      if (!hex) return false;
    }
    return true;
  }
  function normalizePkHex(s) {
    s = String(s || "").replace(/\s+/g, "");
    if (s.slice(0, 2) === "0x" || s.slice(0, 2) === "0X") s = s.slice(2);
    if (s.length !== 64 || !/^[0-9a-fA-F]{64}$/.test(s)) throw new Error("agent 私钥长度不对");
    return "0x" + s;
  }
  function extractPkFromJson(obj) {
    if (!obj || typeof obj !== "object") return "";
    var keys = ["privateKey", "private_key", "hex", "key", "secret", "pk", "privKey"];
    var i, k, v;
    for (i = 0; i < keys.length; i++) {
      k = keys[i];
      v = obj[k];
      if (typeof v === "string" && v) return v;
    }
    for (i = 0; i < keys.length; i++) {
      k = keys[i];
      v = obj[k];
      if (v && (v instanceof Uint8Array || Array.isArray(v)) && v.length === 32) {
        return "0x" + bytesToHexPlain(v instanceof Uint8Array ? v : new Uint8Array(v));
      }
    }
    if (obj.wallet && typeof obj.wallet === "object") return extractPkFromJson(obj.wallet);
    if (obj.agent && typeof obj.agent === "object") return extractPkFromJson(obj.agent);
    return "";
  }
  function parseDecryptedAgentKey(raw) {
    var u8 = raw instanceof Uint8Array ? raw : new Uint8Array(raw);
    var text = "";
    try { text = new TextDecoder().decode(u8); } catch (eDec) { text = ""; }
    var trimmed = String(text || "").replace(/^\s+|\s+$/g, "");
    if (trimmed.charAt(0) === "{") {
      try {
        var obj = JSON.parse(trimmed);
        var fromJson = extractPkFromJson(obj);
        if (fromJson) return normalizePkHex(fromJson);
      } catch (eJ) {}
    }
    if (trimmed.charAt(0) === "\"") {
      try {
        var qs = JSON.parse(trimmed);
        if (typeof qs === "string" && qs) return normalizePkHex(qs);
      } catch (eQ) {}
    }
    if (isAsciiHexKeyBytes(u8)) return normalizePkHex(trimmed);
    if (u8.length === 32) return "0x" + bytesToHexPlain(u8);
    if (/^(0x|0X)?[0-9a-fA-F]{64}$/.test(trimmed)) return normalizePkHex(trimmed);
    throw new Error("无法解析本地 agent 私钥格式");
  }
  function agentInExtras(rows, addr) {
    if (!addr || !Array.isArray(rows)) return false;
    var lc = String(addr).toLowerCase();
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var a = row && (row.address || row.agentAddress || row.agent);
      if (a && String(a).toLowerCase() === lc) return true;
    }
    return false;
  }
  async function fetchExtraAgents(user) {
    var u = String(user || currentWallet() || "");
    if (!u) return null;
    try {
      var rows = await hlInfo({ type: "extraAgents", user: u });
      if (Array.isArray(rows)) return rows;
      return null;
    } catch (eX) {
      warn("extraAgents", eX && eX.message);
      return null;
    }
  }
  async function refreshAgentApproved() {
    var user = currentWallet() || state.wallet;
    var addr = state.agentAddress;
    if (!user || !addr) return state.agentApproved;
    var extras = await fetchExtraAgents(user);
    if (extras == null) return state.agentApproved;
    var found = agentInExtras(extras, addr);
    if (found) {
      state.agentApproved = true;
      log("本地 agent " + addr + " extraAgents=已批准 n=" + extras.length);
    } else {
      state.agentApproved = true;
      warn("extraAgents 未列出本地 agent " + addr + " n=" + extras.length + "，仍继续报价");
    }
    return state.agentApproved;
  }
  var agentNeedSiteShown = false;
  function requireApprovedAgent() {
    if (state.agentApproved === false) warn("extraAgents 未批准/未知，不阻断自动报价");
  }
  function showAgentNeedSiteOnce() {
    if (agentNeedSiteShown) {
      warn(AGENT_NEED_SITE);
      return;
    }
    agentNeedSiteShown = true;
    setStatus(AGENT_NEED_SITE, true);
  }
  function exchangeSaysMissing(resp) {
    var blob = "";
    try { blob = JSON.stringify(resp || ""); } catch (eB) { blob = String(resp || ""); }
    return /does not exist/i.test(blob);
  }
  function recoverAddressFromSig(digestHex, sigHex) {
    var hash = hexToBytes(digestHex);
    var h = String(sigHex || "");
    if (h.slice(0, 2) === "0x" || h.slice(0, 2) === "0X") h = h.slice(2);
    if (h.length < 130) throw new Error("签名长度不足，无法恢复地址");
    var compact = h.slice(0, 128);
    var v = parseInt(h.slice(128, 130), 16);
    if (!Number.isFinite(v)) throw new Error("签名 v 无效");
    var recBit = v >= 27 ? v - 27 : v;
    if (recBit > 3) recBit = recBit & 3;
    var pub = nobleSecp.recoverPublicKey(hash, compact, recBit, false);
    var body = pub.length === 65 && pub[0] === 4 ? pub.subarray(1) : pub;
    return toChecksumAddress(bytesToHexPlain(keccak256(body).subarray(12)));
  }
  async function loadLocalAgent() {
    if (state.agentWallet && typeof state.agentWallet.signDigest === "function") return state.agentWallet;
    var user = currentWallet() || entropyWallet();
    if (!user) throw new Error("页面还没读到钱包地址，先在 entropy 连上钱包");
    state.wallet = user;
    var pair = await idbGetMany("entropy-agent", "keys", ["agent:" + user.toLowerCase(), "wrap-key"]);
    var rec = pair[0];
    var wrap = pair[1];
    if (!rec || !wrap) {
      throw new Error("没有本地 agent。请先在 entropy 交易页自己下一笔任意单（生成并批准 agent），再回来点实盘");
    }
    var iv = new Uint8Array(rec.iv);
    var data = new Uint8Array(rec.data);
    var raw = await crypto.subtle.decrypt({ name: "AES-GCM", iv: iv }, wrap, data);
    var pk = parseDecryptedAgentKey(raw);
    var w = makeLocalSigner(pk);
    state.agentWallet = w;
    state.agentAddress = w.address;
    refreshAgentApproved().catch(function (eAp) { warn("extraAgents", eAp && eAp.message); });
    return w;
  }
  async function ensureWallet() {
    var fromEnt = entropyWallet();
    if (fromEnt) state.wallet = fromEnt;
    if (state.wallet) return state.wallet;
    var provider = window.ethereum;
    if (!provider) throw new Error("没有钱包地址。请先在 entropy 页面连接钱包");
    var accs = await provider.request({ method: "eth_requestAccounts" });
    if (!accs || !accs[0]) throw new Error("钱包未授权");
    state.wallet = accs[0];
    return state.wallet;
  }
  var agentPreloadStarted = false;
  function scheduleAgentPreload() {
    if (agentPreloadStarted || state.agentWallet) return;
    agentPreloadStarted = true;
    setTimeout(function () {
      loadLocalAgent().catch(function (eP) { warn("agent 预加载", eP && eP.message); });
    }, 1000);
  }
  function typeHashDomain() {
    return keccak256(utf8Bytes("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"));
  }
  function typeHashAgent() {
    return keccak256(utf8Bytes("Agent(string source,bytes32 connectionId)"));
  }
  function hashEip712Domain() {
    return keccak256(u8concat([
      typeHashDomain(),
      keccak256(utf8Bytes("Exchange")),
      keccak256(utf8Bytes("1")),
      beBig(1337, 32),
      beBig(0, 32)
    ]));
  }
  function hashAgentStruct(connectionId) {
    var cid = hexToBytes(connectionId);
    if (cid.length !== 32) throw new Error("connectionId 必须是 32 字节");
    return keccak256(u8concat([
      typeHashAgent(),
      keccak256(utf8Bytes("a")),
      cid
    ]));
  }
  function eip712Digest(connectionId) {
    return keccak256(u8concat([
      new Uint8Array([0x19, 0x01]),
      hashEip712Domain(),
      hashAgentStruct(connectionId)
    ]));
  }
  function makeLocalSigner(pkHex) {
    var pk = String(pkHex);
    if (pk.slice(0, 2) === "0x" || pk.slice(0, 2) === "0X") pk = pk.slice(2);
    if (pk.length !== 64) throw new Error("agent 私钥长度不对");
    var pub = nobleSecp.getPublicKey(pk, false);
    var body = pub.length === 65 && pub[0] === 4 ? pub.subarray(1) : pub;
    var address = toChecksumAddress(bytesToHexPlain(keccak256(body).subarray(12)));
    return {
      address: address,
      signDigest: async function (hashHex) {
        var hash = hexToBytes(hashHex);
        if (hash.length !== 32) throw new Error("digest 必须 32 字节");
        var rec = await nobleSecp.sign(hash, pk, { recovered: true, der: false });
        var sig = rec[0];
        var recovery = rec[1] | 0;
        var v = recovery < 27 ? recovery + 27 : recovery;
        if (v !== 27 && v !== 28) v = 27 + (recovery & 1);
        var vhex = v.toString(16);
        if (vhex.length < 2) vhex = "0" + vhex;
        var sigHex = "0x" + bytesToHexPlain(sig) + vhex;
        var recovered = recoverAddressFromSig(hashHex, sigHex);
        if (recovered.toLowerCase() !== address.toLowerCase()) {
          var altV = v === 27 ? 28 : 27;
          var altHex = "0x" + bytesToHexPlain(sig) + (altV === 27 ? "1b" : "1c");
          var altRec = recoverAddressFromSig(hashHex, altHex);
          if (altRec.toLowerCase() === address.toLowerCase()) sigHex = altHex;
          else throw new Error("签名恢复地址 " + recovered + " 与本地 agent " + address + " 不一致，已拒绝发单");
        }
        return sigHex;
      }
    };
  }
  function hashConnectionId(action, nonce) {
    return toHex32(keccak256(connectionPayload(action, nonce)));
  }
  function l1TypedData(connectionId) {
    return {
      types: {
        EIP712Domain: [
          { name: "name", type: "string" },
          { name: "version", type: "string" },
          { name: "chainId", type: "uint256" },
          { name: "verifyingContract", type: "address" }
        ],
        Agent: [
          { name: "source", type: "string" },
          { name: "connectionId", type: "bytes32" }
        ]
      },
      primaryType: "Agent",
      domain: { name: "Exchange", version: "1", chainId: 1337, verifyingContract: ZERO },
      message: { source: "a", connectionId: connectionId }
    };
  }
  async function signL1(action, nonce) {
    var connectionId = hashConnectionId(action, nonce);
    var digest = toHex32(eip712Digest(connectionId));
    var agent = await loadLocalAgent();
    var sigHex = await agent.signDigest(digest);
    var recovered = recoverAddressFromSig(digest, sigHex);
    if (recovered.toLowerCase() !== agent.address.toLowerCase()) {
      throw new Error("签名恢复地址 " + recovered + " 与本地 agent " + agent.address + " 不一致，已拒绝发单");
    }
    log("用本地 agent 签 L1，不走钱包 chainId 1337  agent=" + agent.address + " recovered=" + recovered);
    return { signature: splitSig(sigHex), connectionId: connectionId, address: agent.address };
  }
  async function postExchange(action, nonce, signature) {
    return fetchJson(EXCHANGE_URL, { action: action, nonce: nonce, signature: signature }, 8000, "exchange HTTP");
  }
  function nextNonce() {
    var n = nowMs();
    if (state.lastNonce && n <= state.lastNonce) n = state.lastNonce + 1;
    state.lastNonce = n;
    return n;
  }
  async function sendSignedAction(action) {
    await loadLocalAgent();
    refreshAgentApproved().catch(function (eR) { warn("extraAgents", eR && eR.message); });
    var nonce = nextNonce();
    var signed = await signL1(action, nonce);
    var resp = await postExchange(action, nonce, signed.signature);
    if (exchangeSaysMissing(resp)) {
      state.agentApproved = false;
      showAgentNeedSiteOnce();
      throw new Error(AGENT_NEED_SITE);
    }
    return resp;
  }
  function buildCancelAction(cancels) {
    return { type: "cancel", cancels: cancels };
  }
  function pxEqual(a, b) {
    if (a == null || b == null || a === "" || b === "") return false;
    if (String(a) === String(b)) return true;
    var na = Number(a), nb = Number(b);
    return Number.isFinite(na) && Number.isFinite(nb) && na === nb;
  }
  function orderOid(o) {
    if (!o) return null;
    if (o.oid != null) return o.oid;
    if (o.o != null) return o.o;
    return null;
  }
  function orderPx(o) {
    if (!o) return "";
    if (o.limitPx != null) return String(o.limitPx);
    if (o.px != null) return String(o.px);
    if (o.p != null) return String(o.p);
    return "";
  }
  function orderSideBuy(o) {
    if (!o) return null;
    var side = o.side;
    if (side === "B" || side === "b") return true;
    if (side === "A" || side === "S" || side === "a" || side === "s") return false;
    if (o.b === true) return true;
    if (o.b === false) return false;
    return null;
  }
  function isLimitOrder(o) {
    if (!o) return false;
    if (o.t && o.t.limit) return true;
    var tif = o.tif != null ? String(o.tif) : "";
    var ot = o.orderType != null ? String(o.orderType) : "";
    if (/limit|alo|gtc|ioc|gtd/i.test(tif) || /limit|alo/i.test(ot)) return true;
    if (o.limitPx != null) return true;
    return false;
  }
  function coinBase(coin) {
    var c = String(coin || "");
    return c.indexOf(":") >= 0 ? c.split(":").slice(1).join(":") : c;
  }
  function matchesCoin(o, coin) {
    if (!o) return false;
    coin = String(coin || "");
    var base = coinBase(coin);
    var baseLc = base.toLowerCase();
    var ast = state.assets[coin];
    var assetId = ast && ast.assetId != null ? (ast.assetId | 0) : null;
    var raw = String(o.coin != null ? o.coin : "");
    if (/xyz|vntl/i.test(raw)) return false;
    if (raw === coin || (base && raw === base)) return true;
    if (assetId != null && (raw === "@" + assetId || raw === String(assetId))) return true;
    var aid = o.a != null ? o.a : (o.asset != null ? o.asset : null);
    if (aid != null && assetId != null && (aid | 0) === assetId) return true;
    if (raw && base && raw.toLowerCase().indexOf(baseLc) !== -1) return true;
    return false;
  }
  function orderCoin(o) {
    for (var i = 0; i < ALLOWED.length; i++) {
      if (matchesCoin(o, ALLOWED[i])) return ALLOWED[i];
    }
    return "";
  }
  function sleepMs(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }
  function masterUser(wallet) {
    var user = String(wallet || currentWallet() || "");
    var agent = state.agentAddress ? String(state.agentAddress) : "";
    if (agent && user && user.toLowerCase() === agent.toLowerCase()) {
      user = String(currentWallet() || "");
      if (agent && user && user.toLowerCase() === agent.toLowerCase()) {
        throw new Error("拒绝用 agent 地址查挂单");
      }
    }
    return user;
  }
  async function fetchOpenOrders(wallet) {
    try {
      var user = masterUser(wallet);
      if (!user) return null;
      async function query(u) {
        try {
          var r = await hlInfo({ type: "frontendOpenOrders", user: u, dex: DEX });
          if (Array.isArray(r)) return r;
          if (r == null) throw new Error("frontendOpenOrders 空/限流");
        } catch (eFo) {
          warn("frontendOpenOrders 失败，回退 openOrders", eFo && eFo.message);
        }
        try {
          var r2 = await hlInfo({ type: "openOrders", user: u, dex: DEX });
          if (Array.isArray(r2)) return r2;
          if (r2 == null) return null;
          return [];
        } catch (eOo) {
          warn("openOrders 失败", eOo && eOo.message);
          return null;
        }
      }
      var rows = await query(user);
      if (rows == null && user !== user.toLowerCase()) {
        rows = await query(user.toLowerCase());
      }
      return rows;
    } catch (eAll) {
      warn("fetchOpenOrders", eAll && eAll.message);
      return null;
    }
  }
  function coinOpenOrders(opens, coin) {
    var out = [];
    for (var i = 0; i < (opens || []).length; i++) {
      if (matchesCoin(opens[i], coin)) out.push(opens[i]);
    }
    return out;
  }
  function oidsForCoin(opens, coin) {
    var oids = [];
    var seen = {};
    var ours = coinOpenOrders(opens, coin);
    for (var i = 0; i < ours.length; i++) {
      var oid = orderOid(ours[i]);
      if (oid == null) continue;
      var k = String(oid);
      if (seen[k]) continue;
      seen[k] = true;
      oids.push(oid);
    }
    return oids;
  }
  function actionOk(resp) {
    if (!resp) return false;
    if (resp.status && resp.status !== "ok") return false;
    return true;
  }
  function blobText(x) {
    if (x == null) return "";
    if (typeof x === "string") return x;
    if (x && typeof x.message === "string" && x.message) {
      var extra = "";
      try { extra = JSON.stringify(x); } catch (eJ) { extra = String(x); }
      return String(x.message) + " " + extra;
    }
    try { return JSON.stringify(x); } catch (eB) { return String(x); }
  }
  function aloPxReject(err) {
    return /Bad Alo Px|alo px|post-only|would match immediately/i.test(blobText(err));
  }
  function transientAutoErr(err) {
    if (aloPxReject(err)) return true;
    return /429|timeout|network|HTTP 5|超时/i.test(blobText(err));
  }
  function fatalAutoErr(err) {
    var s0 = blobText(err);
    return /没有钱包|没有本地 agent|无法解析本地 agent|不一致，已拒绝发单|页面还没读到钱包|批准 agent/.test(s0);
  }
  function markAloReject(coin, detail) {
    warn("ALO 价被拒，跟新盘口重挂", coin || "", detail && (detail.message || detail));
    if (coin) {
      state.lastQuoted[coin] = "";
      state.needsRequote[coin] = true;
    }
    setStatus("ALO 价被拒，跟新盘口重挂", false);
  }
  function oidSlot(coin) {
    if (!state.oids[coin]) state.oids[coin] = { buy: null, sell: null };
    return state.oids[coin];
  }
  function listCachedOids(coin) {
    var s = oidSlot(coin);
    var out = [];
    var seen = {};
    function add(x) {
      if (x == null) return;
      var k = String(x);
      if (seen[k]) return;
      seen[k] = true;
      out.push(x);
    }
    add(s.buy);
    add(s.sell);
    var extras = state.extraOids[coin] || [];
    for (var i = 0; i < extras.length; i++) add(extras[i]);
    return out;
  }
  function clearOids(coin) {
    state.oids[coin] = { buy: null, sell: null };
    state.extraOids[coin] = [];
    state.lastQuoted[coin] = "";
  }
  function addExtraOid(coin, oid) {
    if (oid == null) return;
    var s = oidSlot(coin);
    if (s.buy === oid || s.sell === oid) return;
    if (!state.extraOids[coin]) state.extraOids[coin] = [];
    var extras = state.extraOids[coin];
    for (var i = 0; i < extras.length; i++) if (String(extras[i]) === String(oid)) return;
    extras.push(oid);
  }
  function quoteKey(px, plan) {
    var k = String(px.buy) + "|" + String(px.sell);
    if (!plan) return k;
    return k + "|" + String(plan.buySz || "") + "|" + String(plan.sellSz || "") + "|" + (plan.wantBuy ? "B" : "") + (plan.wantSell ? "S" : "") + "|" + (plan.buyR ? "1" : "0") + (plan.sellR ? "1" : "0");
  }
  function posSzi(coin) {
    var v = Number(state.pos && state.pos[coin]);
    return Number.isFinite(v) ? v : 0;
  }
  function fmtSzi(n) {
    var x = Number(n);
    if (!Number.isFinite(x)) return "0";
    var t = x.toFixed(8);
    if (t.indexOf(".") !== -1) t = t.replace(/\.?0+$/, "");
    return t || "0";
  }
  function normalizePosCoin(c) {
    c = String(c || "");
    if (ALLOWED.indexOf(c) !== -1) return c;
    if (c.indexOf(":") === -1) {
      var pref = DEX + ":" + c;
      if (ALLOWED.indexOf(pref) !== -1) return pref;
    }
    return normalizeWsCoin(c);
  }
  function parseClearinghouse(json, into, mentioned) {
    var rows = json && json.assetPositions;
    if (!Array.isArray(rows)) return false;
    var i;
    for (i = 0; i < rows.length; i++) {
      var pos = rows[i] && rows[i].position;
      if (!pos) continue;
      var coin = normalizePosCoin(pos.coin);
      if (!coin) continue;
      var szi = Number(pos.szi);
      if (!Number.isFinite(szi)) szi = 0;
      into[coin] = szi;
      if (mentioned) mentioned[coin] = true;
    }
    return true;
  }
  async function refreshPositions(force) {
    var t = nowMs();
    if (!force && state.posTs && (t - state.posTs) < 800) return state.pos;
    var user = "";
    try { user = masterUser(); } catch (eU) { user = currentWallet() || ""; }
    if (!user) return state.pos;
    var next = {};
    var mentioned = {};
    var i;
    for (i = 0; i < ALLOWED.length; i++) next[ALLOWED[i]] = 0;
    var mainOk = false, ioOk = false;
    try {
      var a = await hlInfo({ type: "clearinghouseState", user: user });
      if (a && parseClearinghouse(a, next, mentioned)) mainOk = true;
    } catch (eA) { warn("clearinghouseState", eA && eA.message); }
    try {
      var b = await hlInfo({ type: "clearinghouseState", user: user, dex: DEX });
      if (b && parseClearinghouse(b, next, mentioned)) ioOk = true;
    } catch (eB) { warn("clearinghouseState io", eB && eB.message); }
    if (mainOk || ioOk) {
      if (!ioOk) {
        for (i = 0; i < ALLOWED.length; i++) {
          if (!mentioned[ALLOWED[i]]) next[ALLOWED[i]] = posSzi(ALLOWED[i]);
        }
      }
      state.pos = next;
      state.posTs = nowMs();
    } else if (!force) {
      state.posTs = nowMs();
    }
    return state.pos;
  }
  function reduceSz(absSzi, px, szDecimals, notional) {
    var ntlWired = sizeFromNotional(px, szDecimals, notional);
    var ntlN = Number(ntlWired);
    var abs = Number(absSzi);
    if (!(abs > 0)) throw new Error("数量过小");
    var use = abs < ntlN ? abs : ntlN;
    var wired = roundSz(use, szDecimals);
    if (Number(wired) > abs + 1e-12) {
      var fac = Math.pow(10, szDecimals | 0);
      var floored = Math.floor(abs * fac + 1e-12) / fac;
      if (!(floored > 0)) throw new Error("数量过小");
      wired = floatToWire(floored);
    }
    return wired;
  }
  function flattenPlan(coin, q, px) {
    var szi = posSzi(coin);
    var buySz = sizeFromNotional(px.buy, q.szDecimals, state.notional);
    var sellSz = sizeFromNotional(px.sell, q.szDecimals, state.notional);
    if (szi > 0) {
      try { sellSz = reduceSz(Math.abs(szi), px.sell, q.szDecimals, state.notional); }
      catch (eL) { sellSz = roundSz(Math.abs(szi), q.szDecimals); }
      return { mode: "long", szi: szi, wantBuy: false, wantSell: true, buySz: buySz, sellSz: sellSz, buyR: false, sellR: true };
    }
    if (szi < 0) {
      try { buySz = reduceSz(Math.abs(szi), px.buy, q.szDecimals, state.notional); }
      catch (eS) { buySz = roundSz(Math.abs(szi), q.szDecimals); }
      return { mode: "short", szi: szi, wantBuy: true, wantSell: false, buySz: buySz, sellSz: sellSz, buyR: true, sellR: false };
    }
    return { mode: "flat", szi: 0, wantBuy: true, wantSell: true, buySz: buySz, sellSz: sellSz, buyR: false, sellR: false };
  }
  function flattenStatusBit(coin) {
    var name = String(coin).replace(/^io:/, "");
    var szi = posSzi(coin);
    if (szi > 0) return name + " 多 " + fmtSzi(szi) + " → 只挂卖平 @ask";
    if (szi < 0) return name + " 空 " + fmtSzi(Math.abs(szi)) + " → 只挂买平 @bid";
    return name + " 空仓 双边";
  }
  function statusOid(st) {
    if (!st || typeof st === "string") return null;
    if (st.resting && st.resting.oid != null) return st.resting.oid;
    if (st.filled && st.filled.oid != null) return st.filled.oid;
    if (st.oid != null) return st.oid;
    return null;
  }
  function statusError(st) {
    if (!st) return "";
    if (typeof st === "string") return st;
    if (typeof st.error === "string") return st.error;
    return "";
  }
  function respStatuses(resp) {
    var sts = resp && resp.response && resp.response.data && resp.response.data.statuses;
    return Array.isArray(sts) ? sts : [];
  }
  function ingestSideOid(coin, isBuy, oid) {
    if (oid == null) return;
    var s = oidSlot(coin);
    if (isBuy) s.buy = oid;
    else s.sell = oid;
  }
  function ingestOrderStatuses(coin, orders, resp) {
    var sts = respStatuses(resp);
    var i;
    for (i = 0; i < sts.length; i++) {
      var err = statusError(sts[i]);
      var oid = statusOid(sts[i]);
      var isBuy = orders[i] ? !!orders[i].b : null;
      if (err) {
        warn("下单状态", coin, err);
        continue;
      }
      if (oid != null && isBuy != null) ingestSideOid(coin, isBuy, oid);
    }
  }
  function normalizeWsCoin(c) {
    c = String(c || "");
    if (ALLOWED.indexOf(c) !== -1) return c;
    for (var i = 0; i < ALLOWED.length; i++) {
      if (matchesCoin({ coin: c }, ALLOWED[i])) return ALLOWED[i];
    }
    return "";
  }
  function applyOpenOrders(opens) {
    var byCoin = {};
    var i;
    for (i = 0; i < ALLOWED.length; i++) byCoin[ALLOWED[i]] = { buys: [], sells: [] };
    for (i = 0; i < (opens || []).length; i++) {
      var o = opens[i];
      var coin = orderCoin(o);
      if (!coin) continue;
      var oid = orderOid(o);
      if (oid == null) continue;
      var isBuy = orderSideBuy(o);
      if (isBuy === true) byCoin[coin].buys.push(oid);
      else if (isBuy === false) byCoin[coin].sells.push(oid);
    }
    for (i = 0; i < ALLOWED.length; i++) {
      var c = ALLOWED[i];
      var g = byCoin[c];
      state.oids[c] = {
        buy: g.buys.length ? g.buys[0] : null,
        sell: g.sells.length ? g.sells[0] : null
      };
      state.extraOids[c] = g.buys.slice(1).concat(g.sells.slice(1));
    }
  }
  function applyOrderUpdate(upd) {
    if (!upd) return;
    var o = upd.order || upd;
    var coin = orderCoin(o) || normalizeWsCoin(o.coin);
    if (!coin) return;
    var oid = orderOid(o);
    if (oid == null) return;
    var isBuy = orderSideBuy(o);
    var status = String(upd.status || o.status || "").toLowerCase();
    var gone = /cancel|filled|reject|margin/.test(status);
    var resting = /open|resting/.test(status) || status === "";
    var s = oidSlot(coin);
    if (gone) {
      if (s.buy != null && String(s.buy) === String(oid)) s.buy = null;
      if (s.sell != null && String(s.sell) === String(oid)) s.sell = null;
      var extras = state.extraOids[coin] || [];
      state.extraOids[coin] = extras.filter(function (x) { return String(x) !== String(oid); });
      if (state.requoteBusy[coin] && /cancel/.test(status) && !/filled/.test(status)) return;
      if (autoOn(coin) && /filled/.test(status)) {
        state.lastQuoted[coin] = "";
        state.needsRequote[coin] = true;
        state.posForce = true;
        requoteCoin(coin).catch(function (eF) { warn("成交补挂", eF && eF.message); });
      }
      return;
    }
    if (resting) {
      if (isBuy === true) {
        if (s.buy == null || String(s.buy) === String(oid)) s.buy = oid;
        else addExtraOid(coin, oid);
      } else if (isBuy === false) {
        if (s.sell == null || String(s.sell) === String(oid)) s.sell = oid;
        else addExtraOid(coin, oid);
      }
    }
  }
  function buildModifyAction(modifies) {
    return { type: "batchModify", modifies: modifies };
  }
  function modifyWire(oid, o) {
    return {
      oid: asOid(oid),
      order: { a: o.a | 0, b: !!o.b, p: String(o.p), s: String(o.s), r: !!o.r, t: { limit: { tif: "Alo" } } }
    };
  }
  function wsSend(obj) {
    if (state.ws && state.ws.readyState === 1) {
      try { state.ws.send(JSON.stringify(obj)); } catch (eS) { warn("ws send", eS && eS.message); }
    }
  }
  function subscribeBooks() {
    for (var i = 0; i < ALLOWED.length; i++) {
      wsSend({ method: "subscribe", subscription: { type: "l2Book", coin: ALLOWED[i], nSigFigs: 5 } });
    }
  }
  function subscribeOrders() {
    var user = "";
    try { user = masterUser(); } catch (eU) { user = currentWallet() || ""; }
    if (!user) return;
    if (state.wsOrderUser && state.wsOrderUser.toLowerCase() === user.toLowerCase()) return;
    state.wsOrderUser = user;
    wsSend({ method: "subscribe", subscription: { type: "orderUpdates", user: user } });
  }
  function applyWsBook(coin, levels) {
    coin = normalizeWsCoin(coin) || coin;
    if (ALLOWED.indexOf(coin) === -1) return;
    var bids = (levels && levels[0]) || [];
    var asks = (levels && levels[1]) || [];
    var bidPx = bids[0] && bids[0].px != null ? String(bids[0].px) : "";
    var askPx = asks[0] && asks[0].px != null ? String(asks[0].px) : "";
    var bid = bidPx ? Number(bidPx) : NaN;
    var ask = askPx ? Number(askPx) : NaN;
    var mid = Number.isFinite(bid) && Number.isFinite(ask) ? (bid + ask) / 2 : NaN;
    var q = mergeQuote(coin, { bid: bid, ask: ask, bidPx: bidPx, askPx: askPx, mid: mid, bids: bids, asks: asks, src: "ws" });
    updateCoinCard(coin);
    if (!autoOn(coin)) return;
    try {
      var px = wantedPx(q);
      if (!px) return;
      var key = quoteKey(px, flattenPlan(coin, q, px));
      if (key !== state.lastQuoted[coin]) requoteCoin(coin).catch(function (eR) { warn("ws requote", eR && eR.message); });
    } catch (ePx) {}
  }
  function handleWsMsg(raw) {
    var msg;
    try { msg = JSON.parse(raw); } catch (eJ) { return; }
    state.wsLastMsg = nowMs();
    var ch = msg && msg.channel;
    var data = msg && msg.data;
    if (ch === "pong" || (msg && msg.method === "pong")) return;
    if (ch === "l2Book" && data) {
      applyWsBook(data.coin, data.levels);
      return;
    }
    if (ch === "orderUpdates") {
      var rows = Array.isArray(data) ? data : (data ? [data] : []);
      for (var i = 0; i < rows.length; i++) applyOrderUpdate(rows[i]);
    }
  }
  function connectWs() {
    if (state.ws && (state.ws.readyState === 0 || state.ws.readyState === 1)) return;
    var ws;
    try { ws = new WebSocket(WS_URL); }
    catch (eW) {
      state.wsUp = false;
      warn("ws 连接失败", eW && eW.message);
      return;
    }
    state.ws = ws;
    ws.onopen = function () {
      state.wsUp = true;
      state.wsRetry = 0;
      state.wsLastMsg = nowMs();
      state.wsOrderUser = "";
      subscribeBooks();
      subscribeOrders();
      if (state.wsPing) clearInterval(state.wsPing);
      state.wsPing = setInterval(function () {
        wsSend({ method: "ping" });
      }, 30000);
      log("ws 已连接 l2Book " + ALLOWED.join(" "));
    };
    ws.onmessage = function (ev) { handleWsMsg(ev.data); };
    ws.onerror = function () { state.wsUp = false; };
    ws.onclose = function () {
      state.wsUp = false;
      state.wsOrderUser = "";
      if (state.wsPing) { clearInterval(state.wsPing); state.wsPing = 0; }
      if (state.ws === ws) state.ws = null;
      var wait = Math.min(8000, 400 * Math.pow(2, state.wsRetry || 0));
      state.wsRetry = (state.wsRetry || 0) + 1;
      setTimeout(connectWs, wait);
    };
  }
  async function maybeDeadMan() {
    if (!anyAutoCoin()) return;
    var t = nowMs();
    if (state.lastDeadMan && t - state.lastDeadMan < 8000) return;
    state.lastDeadMan = t;
    try {
      await sendDeadMan(t + 20000);
    } catch (eDm) {
      warn("dead-man 续期失败", eDm && eDm.message);
      state.lastDeadMan = t - 4000;
    }
  }
  async function placeSides(coin, q, px, wantBuy, wantSell, plan) {
    var clamped = clampMakerPx(q, px);
    if (!clamped) {
      warn("盘口交叉或无法做 maker，跳过挂单", coin);
      if (coin) {
        state.lastQuoted[coin] = "";
        state.needsRequote[coin] = true;
      }
      return { skipped: true };
    }
    px = clamped;
    plan = plan || flattenPlan(coin, q, px);
    var orders = [];
    var buySz = plan.buySz;
    var sellSz = plan.sellSz;
    if (wantBuy && plan.wantBuy) orders.push({ a: q.assetId, b: true, p: px.buy, s: buySz, r: !!plan.buyR });
    if (wantSell && plan.wantSell) orders.push({ a: q.assetId, b: false, p: px.sell, s: sellSz, r: !!plan.sellR });
    if (!orders.length) return null;
    var resp = await sendSignedAction(buildOrderAction(orders));
    if (aloPxReject(resp)) {
      markAloReject(coin, resp);
      return { aloReject: true, resp: resp };
    }
    if (!actionOk(resp)) throw new Error("下单失败 " + JSON.stringify(resp));
    ingestOrderStatuses(coin, orders, resp);
    log("挂单 " + coin + (wantBuy && plan.wantBuy ? " 买 " + buySz + " @ " + px.buy + (plan.buyR ? " ro" : "") : "") + (wantSell && plan.wantSell ? " 卖 " + sellSz + " @ " + px.sell + (plan.sellR ? " ro" : "") : ""));
    return resp;
  }
  async function requoteCoin(coin) {
    coin = assertAllowedCoin(coin);
    if (!autoOn(coin)) return;
    if (state.requoteBusy[coin] || state.inFlight) {
      state.needsRequote[coin] = true;
      return;
    }
    state.requoteBusy[coin] = true;
    var deferRequote = false;
    try {
      if (!autoOn(coin)) return;
      var q = state.quotes[coin];
      if (!q || q.assetId == null || !Number.isFinite(q.mid)) q = await ensureQuote(coin);
      if (!q || q.assetId == null) throw new Error("资产未解析: " + coin);
      var forcePos = !!state.posForce;
      state.posForce = false;
      await refreshPositions(forcePos);
      var px = wantedPx(q);
      if (!px) {
        warn("盘口交叉或无法做 maker，跳过", coin);
        state.lastQuoted[coin] = "";
        state.needsRequote[coin] = true;
        deferRequote = true;
        return;
      }
      var plan = flattenPlan(coin, q, px);
      var key = quoteKey(px, plan);
      var slot = oidSlot(coin);
      var extras = state.extraOids[coin] || [];
      if (extras.length) {
        try { await cancelOids(coin, extras.slice()); } catch (eX) { warn("撤多余", eX && eX.message); }
        state.extraOids[coin] = [];
        if (!autoOn(coin)) return;
      }
      var kill = [];
      if (!plan.wantBuy && slot.buy != null) kill.push(slot.buy);
      if (!plan.wantSell && slot.sell != null) kill.push(slot.sell);
      if (kill.length) {
        await cancelOids(coin, kill);
        if (!plan.wantBuy) slot.buy = null;
        if (!plan.wantSell) slot.sell = null;
        if (!autoOn(coin)) return;
      }
      var haveBuy = slot.buy != null;
      var haveSell = slot.sell != null;
      var wantBuy = !!plan.wantBuy;
      var wantSell = !!plan.wantSell;
      if (haveBuy === wantBuy && haveSell === wantSell && key === state.lastQuoted[coin]) {
        await maybeDeadMan();
        return;
      }
      var buySz = plan.buySz;
      var sellSz = plan.sellSz;
      var mods = [];
      var prev = String(state.lastQuoted[coin] || "").split("|");
      if (wantBuy && haveBuy && (!pxEqual(prev[0], px.buy) || String(prev[2] || "") !== String(buySz))) {
        mods.push(modifyWire(slot.buy, { a: q.assetId, b: true, p: px.buy, s: buySz, r: !!plan.buyR }));
      }
      if (wantSell && haveSell && (!pxEqual(prev[1], px.sell) || String(prev[3] || "") !== String(sellSz))) {
        mods.push(modifyWire(slot.sell, { a: q.assetId, b: false, p: px.sell, s: sellSz, r: !!plan.sellR }));
      }
      if (mods.length) {
        if (!autoOn(coin)) return;
        var resp = await sendSignedAction(buildModifyAction(mods));
        if (aloPxReject(resp)) {
          markAloReject(coin, resp);
          deferRequote = true;
          return;
        }
        var sts = respStatuses(resp);
        var i;
        for (i = 0; i < mods.length; i++) {
          var err = statusError(sts[i]);
          if (err || !actionOk(resp)) {
            if (aloPxReject(err) || aloPxReject(resp)) {
              markAloReject(coin, err || resp);
              deferRequote = true;
              return;
            }
            if (mods[i].order.b) slot.buy = null;
            else slot.sell = null;
          } else {
            var oid = statusOid(sts[i]);
            if (oid != null) ingestSideOid(coin, mods[i].order.b, oid);
          }
        }
      }
      if (!autoOn(coin)) return;
      var needBuy = wantBuy && slot.buy == null;
      var needSell = wantSell && slot.sell == null;
      if (needBuy || needSell) {
        var placed = await placeSides(coin, q, px, needBuy, needSell, plan);
        if (placed && (placed.aloReject || placed.skipped)) {
          deferRequote = true;
          return;
        }
      }
      state.lastQuoted[coin] = key;
      log("改价 " + coin + " " + flattenStatusBit(coin) + " 买@" + px.buy + " 卖@" + px.sell);
      setStatus("自动紧贴 " + flattenStatusBit(coin), false);
      await maybeDeadMan();
    } catch (eReq) {
      if (aloPxReject(eReq)) {
        markAloReject(coin, eReq);
        deferRequote = true;
        return;
      }
      warn("requote", coin, eReq && eReq.message);
      throw eReq;
    } finally {
      state.requoteBusy[coin] = false;
      if (!deferRequote && state.needsRequote[coin] && autoOn(coin)) {
        state.needsRequote[coin] = false;
        requoteCoin(coin).catch(function (eN) { warn("needsRequote", eN && eN.message); });
      }
    }
  }
  function asOid(x) {
    var n = Number(x);
    if (!Number.isFinite(n)) throw new Error("非法 oid: " + x);
    n = Math.floor(n);
    if (n < 0) throw new Error("非法 oid: " + x);
    return n;
  }
  function tagCancelResp(resp, extra) {
    var out = resp && typeof resp === "object" ? resp : { status: "ok" };
    extra = extra || {};
    var k;
    for (k in extra) if (Object.prototype.hasOwnProperty.call(extra, k)) out[k] = extra[k];
    return out;
  }
  async function safetyScheduleCancel(extra) {
    extra = extra || {};
    extra.rateLimited = true;
    extra.empty = false;
    try {
      await sendDeadMan(Date.now() + 6000);
      extra.scheduled = true;
      extra.status = extra.status || "ok";
      return tagCancelResp({ status: "ok" }, extra);
    } catch (eDm) {
      warn("flatten scheduleCancel", eDm && eDm.message);
      extra.scheduled = false;
      extra.scheduleErr = String(eDm && eDm.message || eDm || "");
      if (!extra.cancelledCache) {
        throw new Error("info限流且定时撤失败，挂单可能仍在" + (extra.scheduleErr ? "：" + extra.scheduleErr : ""));
      }
      extra.status = extra.status || "ok";
      return tagCancelResp({ status: "ok" }, extra);
    }
  }
  async function cancelOids(coin, oids) {
    if (!oids || !oids.length) return { status: "ok", skipped: true };
    var ast = state.assets[coin];
    if (!ast || ast.assetId == null) throw new Error("资产未解析: " + coin);
    var cancels = [];
    for (var i = 0; i < oids.length; i++) cancels.push({ a: ast.assetId | 0, o: asOid(oids[i]) });
    var resp = await sendSignedAction(buildCancelAction(cancels));
    log("撤单 " + coin + " n=" + oids.length + " oids=" + oids.map(asOid).join(",") + " " + JSON.stringify(resp));
    if (!actionOk(resp)) throw new Error("撤单失败 " + JSON.stringify(resp));
    return resp;
  }
  async function cancelAllForCoin(coin, wallet, opts) {
    coin = assertAllowedCoin(coin);
    opts = opts || {};
    var wantRefetch = opts.refetch !== false;
    if (wantRefetch && !state.assets[coin]) await resolveAssets();
    var cached = listCachedOids(coin);
    var cancelledCache = false;
    var lastResp = null;
    if (cached.length) {
      if (!state.assets[coin] || state.assets[coin].assetId == null) {
        try { await resolveAssets(); } catch (eRes) { warn("resolveAssets", eRes && eRes.message); }
      }
      if (state.assets[coin] && state.assets[coin].assetId != null) {
        lastResp = await cancelOids(coin, cached);
        clearOids(coin);
        cancelledCache = true;
      }
    }
    var user = "";
    try { user = masterUser(wallet); } catch (eU) { user = ""; }
    if (!user) {
      if (cancelledCache) return tagCancelResp(lastResp, { cancelledCache: true, empty: false });
      if (!wantRefetch) return safetyScheduleCancel({ cancelledCache: false, reason: "no-user" });
      return { status: "ok", skipped: true, empty: false, reason: "no-user" };
    }
    async function pullAndCancel(again) {
      var opens = await fetchOpenOrders(user);
      if (opens == null) {
        return safetyScheduleCancel({ cancelledCache: cancelledCache });
      }
      applyOpenOrders(opens);
      var oids = oidsForCoin(opens, coin);
      if (!oids.length) {
        clearOids(coin);
        if (cancelledCache) return tagCancelResp(lastResp, { cancelledCache: true, empty: false, fetched: true });
        return { status: "ok", empty: true, fetched: true };
      }
      lastResp = await cancelOids(coin, oids);
      clearOids(coin);
      cancelledCache = true;
      if (again) return pullAndCancel(false);
      return tagCancelResp(lastResp, { cancelledCache: true, empty: false, fetched: true });
    }
    if (!wantRefetch) return pullAndCancel(false);
    return pullAndCancel(true);
  }
  async function sendDeadMan(timeMs) {
    var action = { type: "scheduleCancel" };
    if (timeMs != null) {
      var t = Math.floor(Number(timeMs));
      if (!(t - Date.now() >= 5000)) t = Date.now() + 6000;
      action.time = Math.floor(t);
    }
    var resp = await sendSignedAction(action);
    log(timeMs != null ? "dead-man scheduleCancel +" + Math.round((action.time - Date.now()) / 1000) + "s 账户级" : "dead-man scheduleCancel 已清除");
    return resp;
  }
  async function cancelThenAlo(coin, q) {
    return requoteCoin(coin);
  }
  async function cancelCoinOrders(coin) {
    if (state.inFlight) return;
    state.inFlight = true;
    setLiveBusy(true);
    setStatus("撤单中…", false);
    try {
      coin = assertAllowedCoin(coin);
      var resp = await cancelAllForCoin(coin, currentWallet(), { refetch: true });
      if (resp && resp.rateLimited) setStatus("已撤缓存单号，info限流，已提交 6s 账户级定时撤（所有市场）", false);
      else if (resp && resp.empty) setStatus(coin + " 没有挂单可撤", false);
      else setStatus("已撤 " + coin + " " + JSON.stringify(resp && resp.response ? resp.response : resp), resp && resp.status && resp.status !== "ok");
      return resp;
    } finally {
      state.inFlight = false;
      setLiveBusy(false);
    }
  }
  async function cancelAllDesk() {
    if (state.inFlight) return;
    state.inFlight = true;
    setLiveBusy(true);
    setStatus("全部撤单中…", false);
    try {
      var wallet = currentWallet();
      if (!wallet) throw new Error("没有钱包地址");
      if (!state.assets["io:ANTH"]) await resolveAssets();
      var anyLimited = false, allEmpty = true, lastDesk = null;
      for (var i = 0; i < ALLOWED.length; i++) {
        lastDesk = await cancelAllForCoin(ALLOWED[i], wallet, { refetch: true });
        if (lastDesk && lastDesk.rateLimited) anyLimited = true;
        if (!(lastDesk && lastDesk.empty)) allEmpty = false;
      }
      if (anyLimited) setStatus("已撤缓存单号，info限流，已提交 6s 账户级定时撤（所有市场）", false);
      else if (allEmpty) setStatus("没有挂单可撤", false);
      else setStatus("已全部撤单", false);
    } finally {
      state.inFlight = false;
      setLiveBusy(false);
    }
  }
  async function clearDeadManAndCancelAll(coins) {
    var list = coins || ALLOWED;
    var wallet = currentWallet();
    var anyLimited = false;
    var anyOk = false;
    for (var i = 0; i < list.length; i++) {
      state.needsRequote[list[i]] = false;
      try {
        var r = await cancelAllForCoin(list[i], wallet, { refetch: true });
        if (r && r.rateLimited) anyLimited = true;
        if (r && (r.scheduled || r.fetched || r.cancelledCache || r.empty || actionOk(r))) anyOk = true;
      } catch (eC) { warn("停止撤单", list[i], eC && eC.message); }
    }
    var scheduled = false;
    try {
      if (anyLimited) await sendDeadMan(Date.now() + 6000);
      else await sendDeadMan();
      scheduled = true;
    } catch (eDm) {
      warn("flatten scheduleCancel", eDm && eDm.message);
    }
    if (!anyOk && !scheduled) throw new Error("停止撤单失败：info限流且定时撤失败，挂单可能仍在");
    return { status: "ok", rateLimited: anyLimited, scheduled: scheduled, empty: false };
  }
  function stopAutoMm(msg, isErr) {
    var wasOn = [];
    for (var i = 0; i < ALLOWED.length; i++) {
      if (state.autoCoins[ALLOWED[i]]) wasOn.push(ALLOWED[i]);
    }
    var was = !!state.autoMm || wasOn.length > 0;
    for (var j = 0; j < ALLOWED.length; j++) {
      state.autoCoins[ALLOWED[j]] = false;
      state.needsRequote[ALLOWED[j]] = false;
    }
    state.autoMm = false;
    updateAutoBits();
    if (!was) {
      if (msg) setStatus(msg, !!isErr);
      return;
    }
    Promise.resolve().then(function () {
      return clearDeadManAndCancelAll(wasOn);
    }).then(function (resp) {
      var out = (resp && resp.rateLimited) ? "已撤缓存单号，info限流，已提交 6s 账户级定时撤（所有市场）" : "已停止并撤单";
      if (isErr && msg) out += "：" + msg;
      setStatus(out, !!isErr);
    }).catch(function (eS) {
      var em = String(eS && eS.message || eS || "");
      if (/429/.test(em) || /限流/.test(em)) setStatus("已撤缓存单号，info限流，已提交 6s 账户级定时撤（所有市场）", false);
      else setStatus("停止撤单失败: " + em, true);
    });
  }
  function setAutoCoin(coin, on) {
    coin = assertAllowedCoin(coin);
    on = !!on;
    if (!on) {
      var was = autoOn(coin);
      state.autoCoins[coin] = false;
      state.needsRequote[coin] = false;
      syncAutoMm();
      updateAutoBits();
      if (!was) return;
      var last = !state.autoMm;
      Promise.resolve().then(function () {
        return cancelAllForCoin(coin, currentWallet(), { refetch: true });
      }).then(function (resp) {
        if (last) {
          var dm = (resp && resp.rateLimited) ? sendDeadMan(Date.now() + 6000) : sendDeadMan();
          return dm.catch(function (eDm) {
            warn("flatten scheduleCancel", eDm && eDm.message);
          }).then(function () { return resp; });
        }
        return resp;
      }).then(function (resp) {
        if (resp && resp.rateLimited) setStatus(coin + " 已撤缓存单号，info限流，已提交 6s 账户级定时撤（所有市场）", false);
        else setStatus(coin + " 已停止并撤单", false);
      }).catch(function (eS) {
        var em = String(eS && eS.message || eS || "");
        if (/429/.test(em) || /限流/.test(em)) setStatus(coin + " 已撤缓存单号，info限流，已提交 6s 账户级定时撤（所有市场）", false);
        else setStatus("停止撤单失败: " + em, true);
      });
      return;
    }
    if (autoOn(coin)) { updateAutoBits(); return; }
    loadLocalAgent().then(function () {
      refreshAgentApproved().catch(function (eR) { warn("extraAgents", eR && eR.message); });
      state.autoCoins[coin] = true;
      state.lastQuoted[coin] = "";
      state.needsRequote[coin] = true;
      syncAutoMm();
      setStatus(coin + " 自动紧贴已开启·空仓双边 / 有仓只平", false);
      updateAutoBits();
      connectWs();
      return requoteCoin(coin);
    }).catch(function (eA) {
      var started = autoOn(coin);
      state.autoCoins[coin] = false;
      state.needsRequote[coin] = false;
      syncAutoMm();
      updateAutoBits();
      setStatus(String(eA.message || eA), true);
      if (started) {
        var lastOff = !state.autoMm;
        cancelAllForCoin(coin, currentWallet(), { refetch: true }).then(function (rOff) {
          if (!lastOff) return;
          if (rOff && rOff.rateLimited) return sendDeadMan(Date.now() + 6000);
          return sendDeadMan();
        }).catch(function (eC) { warn("开启失败撤单", eC && eC.message); });
      }
    });
  }
  function setAutoMm(on) {
    on = !!on;
    if (!on) {
      if (state.autoMm || anyAutoCoin()) stopAutoMm("", false);
      else {
        for (var z = 0; z < ALLOWED.length; z++) state.autoCoins[ALLOWED[z]] = false;
        state.autoMm = false;
        updateAutoBits();
      }
      return;
    }
    var missing = [];
    for (var j = 0; j < ALLOWED.length; j++) {
      if (!autoOn(ALLOWED[j])) missing.push(ALLOWED[j]);
    }
    if (!missing.length) { updateAutoBits(); return; }
    loadLocalAgent().then(function () {
      refreshAgentApproved().catch(function (eR) { warn("extraAgents", eR && eR.message); });
      for (var k = 0; k < missing.length; k++) {
        state.autoCoins[missing[k]] = true;
        state.lastQuoted[missing[k]] = "";
        state.needsRequote[missing[k]] = true;
      }
      syncAutoMm();
      setStatus("自动紧贴已开启·空仓双边 / 有仓只平", false);
      updateAutoBits();
      connectWs();
      return autoMmTick();
    }).catch(function (eA) {
      for (var m = 0; m < missing.length; m++) {
        state.autoCoins[missing[m]] = false;
        state.needsRequote[missing[m]] = false;
      }
      syncAutoMm();
      setStatus(String(eA.message || eA), true);
      updateAutoBits();
    });
  }
  async function autoMmTick() {
    if (!state.autoMm) return;
    if (state.autoBusy) return;
    state.autoBusy = true;
    try {
      var wallet = currentWallet();
      if (!wallet) {
        stopAutoMm("自动紧贴已关闭：没有钱包地址", true);
        return;
      }
      try {
        if (!state.agentWallet) await loadLocalAgent();
      } catch (eAg) {
        stopAutoMm(String(eAg.message || eAg), true);
        return;
      }
      connectWs();
      subscribeOrders();
      var now = nowMs();
      var wsFresh = state.wsUp && state.wsLastMsg && now - state.wsLastMsg < 15000;
      for (var i = 0; i < ALLOWED.length; i++) {
        if (!state.autoMm) return;
        var coin = ALLOWED[i];
        if (!autoOn(coin)) continue;
        if (!wsFresh) {
          var q = await ensureQuote(coin);
          if (!q || q.assetId == null) throw new Error("资产未解析: " + coin);
        }
        var need = state.lastQuoted[coin] === "" || state.needsRequote[coin];
        if (!need && state.quotes[coin]) {
          try {
            var qn = state.quotes[coin];
            var pn = wantedPx(qn);
            if (!pn) need = state.lastQuoted[coin] !== "";
            else if (quoteKey(pn, flattenPlan(coin, qn, pn)) !== state.lastQuoted[coin]) need = true;
          } catch (ePx0) { need = !wsFresh; }
        }
        if (need) await requoteCoin(coin);
      }
      if (now - (state.lastReconcile || 0) > 30000) {
        try {
          var opens = await fetchOpenOrders(wallet);
          if (opens == null) {
            warn("调和挂单跳过：info 失败/限流，保留 oid 缓存");
            state.lastReconcile = now;
          } else {
          applyOpenOrders(opens);
          state.lastReconcile = now;
          for (var j = 0; j < ALLOWED.length; j++) {
            if (!autoOn(ALLOWED[j])) continue;
            var ex = state.extraOids[ALLOWED[j]] || [];
            if (ex.length) {
              log("多余挂单 " + ALLOWED[j] + " n=" + ex.length);
              await requoteCoin(ALLOWED[j]);
            }
          }
          }
        } catch (eOrd) {
          warn("调和挂单", eOrd && eOrd.message);
        }
      }
      if (state.autoMm) {
        var bits = [];
        for (var b = 0; b < ALLOWED.length; b++) {
          if (!autoOn(ALLOWED[b])) continue;
          bits.push(flattenStatusBit(ALLOWED[b]));
        }
        if (bits.length && state.lastOk !== "ALO 价被拒，跟新盘口重挂") setStatus("自动紧贴 " + bits.join(" · "), false);
        await maybeDeadMan();
      }
    } catch (eAuto) {
      if (aloPxReject(eAuto) || transientAutoErr(eAuto)) {
        if (aloPxReject(eAuto)) markAloReject("", eAuto);
        else warn("autoMm 暂缓", eAuto && eAuto.message);
      } else if (fatalAutoErr(eAuto)) {
        stopAutoMm("自动紧贴已关闭：" + String(eAuto.message || eAuto), true);
      } else {
        warn("autoMm 继续", eAuto && eAuto.message);
      }
    } finally {
      state.autoBusy = false;
    }
  }
  async function liveBoth(coin) {
    if (state.inFlight) return;
    state.inFlight = true;
    setLiveBusy(true);
    setStatus("提交中…", false);
    try {
      coin = assertAllowedCoin(coin);
      var q = await ensureQuote(coin);
      if (!q || q.assetId == null) throw new Error("资产未解析: " + coin);
      var wallet = currentWallet();
      if (!wallet) throw new Error("没有钱包地址");
      await refreshPositions(true);
      var px = wantedPx(q);
      if (!px) {
        warn("盘口交叉或无法做 maker，跳过", coin);
        setStatus("盘口交叉，稍后重挂", false);
        return;
      }
      var plan = flattenPlan(coin, q, px);
      var buySz = plan.buySz;
      var sellSz = plan.sellSz;
      var slot = oidSlot(coin);
      var extras = state.extraOids[coin] || [];
      if (extras.length) {
        try { await cancelOids(coin, extras.slice()); } catch (eX) { warn("撤多余", eX && eX.message); }
        state.extraOids[coin] = [];
      }
      var resp;
      if (slot.buy != null && slot.sell != null) {
        var killLb = [];
        if (!plan.wantBuy && slot.buy != null) killLb.push(slot.buy);
        if (!plan.wantSell && slot.sell != null) killLb.push(slot.sell);
        if (killLb.length) {
          await cancelOids(coin, killLb);
          if (!plan.wantBuy) slot.buy = null;
          if (!plan.wantSell) slot.sell = null;
        }
        var mods = [];
        if (plan.wantBuy && slot.buy != null) mods.push(modifyWire(slot.buy, { a: q.assetId, b: true, p: px.buy, s: buySz, r: !!plan.buyR }));
        if (plan.wantSell && slot.sell != null) mods.push(modifyWire(slot.sell, { a: q.assetId, b: false, p: px.sell, s: sellSz, r: !!plan.sellR }));
        setStatus("正在改价 " + flattenStatusBit(coin), false);
        if (mods.length) {
          resp = await sendSignedAction(buildModifyAction(mods));
          if (aloPxReject(resp)) {
            markAloReject(coin, resp);
            return;
          }
          var sts = respStatuses(resp);
          var mi0;
          for (mi0 = 0; mi0 < mods.length; mi0++) {
            if (!actionOk(resp) || statusError(sts[mi0])) {
              if (aloPxReject(statusError(sts[mi0])) || aloPxReject(resp)) {
                markAloReject(coin, statusError(sts[mi0]) || resp);
                return;
              }
              if (mods[mi0].order.b) slot.buy = null;
              else slot.sell = null;
            }
          }
        }
        var needBuy = plan.wantBuy && slot.buy == null;
        var needSell = plan.wantSell && slot.sell == null;
        if (needBuy || needSell) {
          resp = await placeSides(coin, q, px, needBuy, needSell, plan);
          if (resp && (resp.aloReject || resp.skipped)) return;
        }
      } else {
        var hadBuy = slot.buy != null;
        var hadSell = slot.sell != null;
        var mods2 = [];
        var killLb2 = [];
        if (!plan.wantBuy && slot.buy != null) killLb2.push(slot.buy);
        if (!plan.wantSell && slot.sell != null) killLb2.push(slot.sell);
        if (killLb2.length) {
          await cancelOids(coin, killLb2);
          if (!plan.wantBuy) slot.buy = null;
          if (!plan.wantSell) slot.sell = null;
          hadBuy = slot.buy != null;
          hadSell = slot.sell != null;
        }
        if (hadBuy && plan.wantBuy) mods2.push(modifyWire(slot.buy, { a: q.assetId, b: true, p: px.buy, s: buySz, r: !!plan.buyR }));
        if (hadSell && plan.wantSell) mods2.push(modifyWire(slot.sell, { a: q.assetId, b: false, p: px.sell, s: sellSz, r: !!plan.sellR }));
        if (mods2.length) {
          resp = await sendSignedAction(buildModifyAction(mods2));
          if (aloPxReject(resp)) {
            markAloReject(coin, resp);
            return;
          }
          var sts2 = respStatuses(resp);
          for (var mi = 0; mi < mods2.length; mi++) {
            if (!actionOk(resp) || statusError(sts2[mi])) {
              if (aloPxReject(statusError(sts2[mi])) || aloPxReject(resp)) {
                markAloReject(coin, statusError(sts2[mi]) || resp);
                return;
              }
              if (mods2[mi].order.b) slot.buy = null;
              else slot.sell = null;
            }
          }
        }
        if ((plan.wantBuy && slot.buy == null) || (plan.wantSell && slot.sell == null)) {
          resp = await placeSides(coin, q, px, plan.wantBuy && slot.buy == null, plan.wantSell && slot.sell == null, plan);
          if (resp && (resp.aloReject || resp.skipped)) return;
        }
      }
      if (resp && (resp.aloReject || resp.skipped)) return;
      state.lastQuoted[coin] = quoteKey(px, plan);
      log("实盘回报 " + JSON.stringify(resp));
      setStatus("实盘 " + flattenStatusBit(coin) + " " + JSON.stringify(resp && resp.response ? resp.response : resp), false);
      return resp;
    } catch (eLb) {
      if (aloPxReject(eLb)) {
        markAloReject(coin, eLb);
        return;
      }
      throw eLb;
    } finally {
      state.inFlight = false;
      setLiveBusy(false);
    }
  }
  async function liveOrder(coin, isBuy) {
    if (state.inFlight) return;
    state.inFlight = true;
    setLiveBusy(true);
    setStatus("提交中…", false);
    await yieldUi();
    try {
      coin = assertAllowedCoin(coin);
      var q = await ensureQuote(coin);
      if (!q || q.assetId == null) throw new Error("资产未解析: " + coin);
      var wallet = currentWallet();
      if (!wallet) throw new Error("没有钱包地址");
      await refreshPositions(true);
      var px = wantedPx(q);
      if (!px) throw new Error("盘口交叉，无法做 maker");
      var plan = flattenPlan(coin, q, px);
      if (isBuy && !plan.wantBuy) throw new Error(coin + " 已有多仓，只允许 reduce-only 卖平");
      if (!isBuy && !plan.wantSell) throw new Error(coin + " 已有空仓，只允许 reduce-only 买平");
      await cancelAllForCoin(coin, wallet, { refetch: false });
      var p = isBuy ? px.buy : px.sell;
      var sz = isBuy ? plan.buySz : plan.sellSz;
      var rOnly = isBuy ? !!plan.buyR : !!plan.sellR;
      var action = buildOrderAction([{ a: q.assetId, b: !!isBuy, p: p, s: sz, r: rOnly }]);
      log("实盘签名", coin, isBuy ? "买" : "卖", sz, "@", p, rOnly ? "ro" : "alo", "asset", q.assetId);
      var resp = await sendSignedAction(action);
      if (aloPxReject(resp)) {
        markAloReject(coin, resp);
        return;
      }
      if (!actionOk(resp)) throw new Error("下单失败 " + JSON.stringify(resp));
      ingestOrderStatuses(coin, [{ a: q.assetId, b: !!isBuy, p: p, s: sz, r: rOnly }], resp);
      log("实盘回报 " + JSON.stringify(resp));
      setStatus("实盘 " + flattenStatusBit(coin) + " " + (isBuy ? "买" : "卖") + (rOnly ? " ro" : "") + " " + JSON.stringify(resp && resp.response ? resp.response : resp), false);
      render();
      return resp;
    } finally {
      state.inFlight = false;
      setLiveBusy(false);
    }
  }
  async function approveBuilder() {
    var addr = await ensureWallet();
    var chainHex = await window.ethereum.request({ method: "eth_chainId" });
    var nonce = nowMs();
    var action = { type: "approveBuilderFee", hyperliquidChain: "Mainnet", signatureChainId: chainHex, maxFeeRate: "0%", builder: BUILDER_LC, nonce: nonce };
    var typed = {
      types: {
        EIP712Domain: [{ name: "name", type: "string" }, { name: "version", type: "string" }, { name: "chainId", type: "uint256" }, { name: "verifyingContract", type: "address" }],
        "HyperliquidTransaction:ApproveBuilderFee": [{ name: "hyperliquidChain", type: "string" }, { name: "maxFeeRate", type: "string" }, { name: "builder", type: "address" }, { name: "nonce", type: "uint64" }]
      },
      primaryType: "HyperliquidTransaction:ApproveBuilderFee",
      domain: { name: "HyperliquidSignTransaction", version: "1", chainId: parseInt(chainHex, 16), verifyingContract: ZERO },
      message: { hyperliquidChain: "Mainnet", maxFeeRate: "0%", builder: BUILDER_LC, nonce: nonce }
    };
    var sigHex = await window.ethereum.request({ method: "eth_signTypedData_v4", params: [addr, JSON.stringify(typed)] });
    var resp = await postExchange(action, nonce, splitSig(sigHex));
    log("批准 builder " + JSON.stringify(resp));
    setStatus("已请求批准 Builder", false);
    return resp;
  }
  function setStatus(msg, isErr) {
    if (isErr) { state.lastErr = msg; warn(msg); } else { state.lastOk = msg; state.lastErr = ""; }
    var node = document.getElementById("ed-status");
    if (node) { node.textContent = msg; node.style.color = isErr ? "#f87171" : "#86efac"; }
  }
  function openTrade(coin) {
    coin = assertAllowedCoin(coin);
    window.open("https://entropy.io/trade/" + coin, "_blank", "noopener");
  }
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "style" && typeof attrs[k] === "object") Object.assign(n.style, attrs[k]);
        else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), attrs[k]);
        else if (k === "text") n.textContent = attrs[k];
        else n.setAttribute(k, attrs[k]);
      });
    }
    (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }
  function coinDomId(coin, part) {
    return "ed-" + String(coin).replace(":", "-") + "-" + part;
  }
  function buildCoinCard(coin) {
    var wrap = el("div", { class: "ed-card", id: coinDomId(coin, "card") });
    var autoLabC = el("label", { id: coinDomId(coin, "autolab") });
    var autoCkC = el("input", { type: "checkbox", id: coinDomId(coin, "auto") });
    autoCkC.checked = !!state.autoCoins[coin];
    autoCkC.addEventListener("change", function () { setAutoCoin(coin, !!autoCkC.checked); });
    autoLabC.appendChild(autoCkC);
    autoLabC.appendChild(document.createTextNode(" 自动紧贴"));
    wrap.appendChild(el("div", { class: "ed-card-h" }, [
      el("b", { text: coin }),
      autoLabC,
      el("a", { text: "打开", href: "https://entropy.io/trade/" + coin, target: "_blank", rel: "noopener" }),
      el("span", { class: "ed-muted", id: coinDomId(coin, "assetid"), text: "id …" })
    ]));
    var grid = el("div", { class: "ed-grid" });
    [[ "买一", "bid", "#4ade80" ], [ "卖一", "ask", "#f87171" ], [ "中间价", "mid", "#e5e7eb" ], [ "点差", "spread", "#fbbf24" ], [ "标记价", "mark", "#93c5fd" ], [ "24h额", "vol", "#c4b5fd" ]].forEach(function (r) {
      grid.appendChild(el("div", { class: "ed-kv" }, [el("span", { class: "ed-k", text: r[0] }), el("span", { class: "ed-v", id: coinDomId(coin, r[1]), text: "—", style: { color: r[2] } })]));
    });
    wrap.appendChild(grid);
    var btns = el("div", { class: "ed-btns" });
    var bPaper = el("button", { type: "button", text: "实盘双边 ALO", class: "ed-btn ed-liveboth", id: coinDomId(coin, "liveboth") });
    var bBuy = el("button", { type: "button", text: "实盘买 ALO", class: "ed-btn ed-buy" });
    var bSell = el("button", { type: "button", text: "实盘卖 ALO", class: "ed-btn ed-sell" });
    var bCancel = el("button", { type: "button", text: "撤该币挂单", class: "ed-btn ed-cancel" });
    bPaper.addEventListener("click", function () { liveBoth(coin).catch(function (e2) { setStatus(String(e2.message || e2), true); }); });
    bBuy.addEventListener("click", function () { liveOrder(coin, true).catch(function (e3) { setStatus(String(e3.message || e3), true); }); });
    bSell.addEventListener("click", function () { liveOrder(coin, false).catch(function (e4) { setStatus(String(e4.message || e4), true); }); });
    bCancel.addEventListener("click", function () { cancelCoinOrders(coin).catch(function (eC) { setStatus(String(eC.message || eC), true); }); });
    btns.appendChild(bPaper); btns.appendChild(bBuy); btns.appendChild(bSell);
    wrap.appendChild(btns);
    var cRow = el("div", { class: "ed-btns" });
    cRow.appendChild(bCancel);
    wrap.appendChild(cRow);
    return wrap;
  }
  function updateCoinCard(coin) {
    var q = state.quotes[coin] || {};
    var a = state.assets[coin] || {};
    function set(part, text) {
      var n = document.getElementById(coinDomId(coin, part));
      if (n) n.textContent = text;
    }
    set("assetid", "id " + (a.assetId != null ? a.assetId : "…"));
    set("bid", fmtNum(q.bid, 4));
    set("ask", fmtNum(q.ask, 4));
    set("mid", fmtNum(q.mid, 4));
    set("spread", Number.isFinite(q.spread) ? fmtNum(q.spread, 4) : "—");
    set("mark", fmtNum(q.mark, 4));
    set("vol", fmtUsd(q.vol24h));
  }
  function updatePaperLog() {
    var logBox = document.getElementById("ed-log");
    if (!logBox) return;
    logBox.textContent = "";
    state.paperLog.slice(0, 6).forEach(function (p) {
      logBox.appendChild(el("div", { text: new Date(p.ts).toLocaleTimeString() + " 纸面 " + p.coin + " " + (p.side === "B" ? "买" : "卖") + " " + p.s + " @ " + p.p }));
    });
  }

  function panelCss() {
    return "#entropy-desk-root{position:fixed;right:16px;bottom:16px;z-index:2147483646;width:360px;max-height:86vh;overflow:auto;background:#0b1220;color:#e5e7eb;border:1px solid #1f2937;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.45);font:12px/1.4 ui-sans-serif,system-ui,sans-serif;padding:10px}" +
      "#entropy-desk-root *{box-sizing:border-box}#entropy-desk-root a{color:#93c5fd;text-decoration:none}" +
      "#entropy-desk-root .ed-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;cursor:move}" +
      "#entropy-desk-root .ed-title{font-weight:700;font-size:13px}#entropy-desk-root .ed-muted{color:#9ca3af}" +
      "#entropy-desk-root .ed-card{background:#111827;border:1px solid #1f2937;border-radius:8px;padding:8px;margin:6px 0}" +
      "#entropy-desk-root .ed-card-h{display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap}#entropy-desk-root .ed-card-h label{display:inline-flex;align-items:center;gap:2px;margin-left:auto;cursor:pointer;font-size:11px;user-select:none}" +
      "#entropy-desk-root .ed-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px 6px}" +
      "#entropy-desk-root .ed-kv{background:#0b1220;border-radius:6px;padding:4px 6px}" +
      "#entropy-desk-root .ed-k{display:block;color:#9ca3af;font-size:10px}" +
      "#entropy-desk-root .ed-v{font-variant-numeric:tabular-nums;font-weight:600}" +
      "#entropy-desk-root .ed-btns{display:flex;gap:4px;margin-top:6px}" +
      "#entropy-desk-root .ed-btn{flex:1;border:0;border-radius:6px;padding:5px 4px;cursor:pointer;color:#fff;font-weight:600;font-size:11px}" +
      "#entropy-desk-root .ed-btn[disabled]{opacity:.55;cursor:wait}" +
      "#entropy-desk-root .ed-paper{background:#334155}#entropy-desk-root .ed-liveboth{background:#1d4ed8}#entropy-desk-root .ed-buy{background:#166534}#entropy-desk-root .ed-sell{background:#7f1d1d}#entropy-desk-root .ed-cancel{background:#78350f}#entropy-desk-root .ed-cancelall{background:#7c2d12}" +
      "#entropy-desk-root .ed-row{display:flex;gap:6px;align-items:center;margin:4px 0;flex-wrap:wrap}" +
      "#entropy-desk-root input[type=number]{width:88px;background:#020617;border:1px solid #334155;color:#e5e7eb;border-radius:6px;padding:3px 6px}" +
      "#entropy-desk-root .ed-fees{background:#111827;border-radius:8px;padding:6px 8px;color:#cbd5e1;white-space:pre-wrap;font-size:11px}" +
      "#entropy-desk-root .ed-log{max-height:72px;overflow:auto;color:#94a3b8;font-size:10px;margin-top:4px}" +
      "#entropy-desk-root #ed-status{min-height:16px;margin-top:4px;word-break:break-all}" +
      "#entropy-desk-root .ed-warn{color:#fbbf24}";
  }
  function makeDrag(box) {
    var ox = 0, oy = 0, dragging = false;
    box.addEventListener("mousedown", function (e) {
      if (!e.target.closest || !e.target.closest(".ed-top")) return;
      dragging = true;
      ox = e.clientX - box.getBoundingClientRect().left;
      oy = e.clientY - box.getBoundingClientRect().top;
      e.preventDefault();
    });
    window.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      box.style.left = Math.max(8, e.clientX - ox) + "px";
      box.style.top = Math.max(8, e.clientY - oy) + "px";
      box.style.right = "auto";
      box.style.bottom = "auto";
    });
    window.addEventListener("mouseup", function () { dragging = false; });
  }
  function ensurePanel() {
    var box = document.getElementById("entropy-desk-root");
    if (box) return box;
    var style = document.createElement("style");
    style.textContent = panelCss();
    document.documentElement.appendChild(style);
    box = el("div", { id: "entropy-desk-root" });
    document.documentElement.appendChild(box);
    makeDrag(box);
    return box;
  }
  function updateStatusNode() {
    var node = document.getElementById("ed-status");
    if (!node) return;
    var msg = state.lastErr || state.lastOk || (state.wsUp ? "就绪 · WS 盘口" : "就绪 · REST 400ms");
    node.textContent = msg;
    node.style.color = state.lastErr ? "#f87171" : "#86efac";
  }
  function updateLiveBits() {
    var lab = document.getElementById("ed-live-lab");
    if (lab) lab.className = state.liveEnabled ? "ed-warn" : "";
    var ck = document.getElementById("ed-live");
    if (ck && ck.checked !== !!state.liveEnabled) ck.checked = !!state.liveEnabled;
    var mode = document.getElementById("ed-mode");
    if (mode) mode.textContent = "自动紧贴：空仓双边，有仓只挂平仓侧，撤单不排队确认";
    updateAutoBits();
  }
  function updateAutoBits() {
    syncAutoMm();
    var ck = document.getElementById("ed-automm");
    if (ck && ck.checked !== !!state.autoMm) ck.checked = !!state.autoMm;
    var lab = document.getElementById("ed-automm-lab");
    if (lab) lab.className = state.autoMm ? "ed-warn" : "";
    for (var i = 0; i < ALLOWED.length; i++) {
      var c = ALLOWED[i];
      var cck = document.getElementById(coinDomId(c, "auto"));
      if (cck && cck.checked !== !!state.autoCoins[c]) cck.checked = !!state.autoCoins[c];
      var clab = document.getElementById(coinDomId(c, "autolab"));
      if (clab) clab.className = state.autoCoins[c] ? "ed-warn" : "";
    }
  }
  function buildPanelOnce(box) {
    if (box.getAttribute("data-ed-built") === "1") return;
    box.setAttribute("data-ed-built", "1");
    box.appendChild(el("div", { class: "ed-top" }, [el("div", { class: "ed-title", text: "Entropy Desk · ANTH / SNDK  1.5.8" }), el("span", { class: "ed-muted", id: "ed-wallet", text: shortAddr(currentWallet()) })]));
    box.appendChild(el("div", { class: "ed-fees", text: "净手续费  taker ≈ 0   maker 0\nHL档4 0.028% × HIP-3×2 × growth×0.1 − Entropy自返200%×50%份额" }));
    var inp = el("input", { type: "number", min: "10", step: "1", value: String(state.notional), id: "ed-notional" });
    inp.addEventListener("change", function () { var v = Number(inp.value); if (v > 0) state.notional = v; });
    var lab = el("label", { id: "ed-live-lab" });
    var ck = el("input", { type: "checkbox", id: "ed-live" });
    ck.checked = !!state.liveEnabled;
    ck.addEventListener("change", function () { state.liveEnabled = !!ck.checked; setStatus(state.liveEnabled ? "实盘已开启·点按钮会真下单" : "实盘已关闭（按钮仍会真下单）", false); updateLiveBits(); });
    lab.appendChild(ck);
    lab.appendChild(document.createTextNode(" 实盘已开启·点按钮会真下单"));
    box.appendChild(el("div", { class: "ed-row" }, [el("label", { text: "名义 USD" }), inp, lab]));
    var autoLab = el("label", { id: "ed-automm-lab" });
    var autoCk = el("input", { type: "checkbox", id: "ed-automm" });
    autoCk.checked = !!state.autoMm;
    autoCk.addEventListener("change", function () { setAutoMm(!!autoCk.checked); });
    autoLab.appendChild(autoCk);
    autoLab.appendChild(document.createTextNode(" 全部自动紧贴"));
    box.appendChild(el("div", { class: "ed-row" }, [autoLab]));
    box.appendChild(el("div", { class: "ed-muted", id: "ed-automm-help", text: "空仓双边；有仓只挂 reduce-only 平仓侧" }));
    box.appendChild(el("div", { class: "ed-muted", id: "ed-deadman-note", text: "定时撤是账户级，会动到其它市场手单" }));
    box.appendChild(el("div", { class: "ed-muted", id: "ed-mode", text: "自动紧贴：空仓双边，有仓只挂平仓侧" }));
    ALLOWED.forEach(function (c) { box.appendChild(buildCoinCard(c)); });
    var extra = el("div", { class: "ed-row" });
    var bAppr = el("button", { type: "button", class: "ed-btn ed-paper", text: "批准 Builder(0)" });
    bAppr.addEventListener("click", function () { approveBuilder().catch(function (e5) { setStatus(String(e5.message || e5), true); }); });
    extra.appendChild(bAppr);
    var bAll = el("button", { type: "button", class: "ed-btn ed-cancel ed-cancelall", text: "全部撤单" });
    bAll.addEventListener("click", function () { cancelAllDesk().catch(function (eAll) { setStatus(String(eAll.message || eAll), true); }); });
    extra.appendChild(bAll);
    extra.appendChild(el("span", { class: "ed-muted", text: "本地 agent 签名·不弹 1337" }));
    box.appendChild(extra);
    box.appendChild(el("div", { class: "ed-log", id: "ed-log" }));
    box.appendChild(el("div", { id: "ed-status", text: state.lastErr || state.lastOk || "就绪 · WS 盘口" }));
  }
  function render() {
    var box = ensurePanel();
    buildPanelOnce(box);
    var w = document.getElementById("ed-wallet");
    if (w) w.textContent = shortAddr(currentWallet());
    var inp = document.getElementById("ed-notional");
    if (inp && document.activeElement !== inp) inp.value = String(state.notional);
    updateLiveBits();
    ALLOWED.forEach(updateCoinCard);
    updatePaperLog();
    updateStatusNode();
  }

  async function tick() {
    if (state.polling) return;
    state.polling = true;
    try {
      if (!state.assets["io:ANTH"]) await resolveAssets();
      connectWs();
      var wsFresh = state.wsUp && state.wsLastMsg && nowMs() - state.wsLastMsg < 15000;
      if (!wsFresh) {
        for (var i = 0; i < ALLOWED.length; i++) mergeQuote(ALLOWED[i], await fetchBook(ALLOWED[i]));
      }
      var w = entropyWallet();
      if (w) state.wallet = w;
      subscribeOrders();
      render();
      scheduleAgentPreload();
      await autoMmTick();
    } catch (e7) {
      setStatus(String(e7.message || e7), true);
      render();
    } finally { state.polling = false; }
  }
  window.EntropyDesk = { version: "1.5.8", state: state, fee: FEE, refresh: tick, paperBoth: paperBoth, liveBoth: liveBoth, liveOrder: liveOrder, approveBuilder: approveBuilder, openTrade: openTrade, assertAllowedCoin: assertAllowedCoin, printFees: printFees, setAutoMm: setAutoMm, setAutoCoin: setAutoCoin, cancelCoinOrders: cancelCoinOrders, cancelAllDesk: cancelAllDesk, aloPrices: aloPrices, clampMakerPx: clampMakerPx, aloPxReject: aloPxReject };
  printFees();
  log("已加载 1.5.8。空仓双边 ALO，有仓只挂 reduce-only 平仓侧。盘口变动即改价。ALO 价被拒则跟新盘口重挂。dead-man 20s 账户级，停止时清除。");
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { render(); tick(); });
  else { render(); tick(); }
  setInterval(tick, POLL_MS);
})();
