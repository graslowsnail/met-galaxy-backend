import fs from "fs";
import path from "path";

// ---------- RNG / hashing ----------
export function hash32(...nums: number[]) {
  let h = 2166136261 >>> 0;
  for (const n of nums) {
    let x = (n | 0) >>> 0;
    h ^= x;
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h >>> 0;
}

export function mulberry32(seed: number) {
  let t = seed >>> 0;
  return () => {
    t += 0x6D2B79F5; t >>>= 0;
    let r = Math.imul(t ^ (t >>> 15), 1 | t);
    r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

export function gaussian(rng: () => number) {
  let u = 0, v = 0;
  while (u === 0) u = rng();
  while (v === 0) v = rng();
  return Math.sqrt(-2.0 * Math.log(u)) * Math.cos(2.0 * Math.PI * v);
}

// ---------- math ----------
export const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

export function smoothstep(edge0: number, edge1: number, x: number) {
  const t = Math.max(0, Math.min(1, (x - edge0) / Math.max(1e-9, edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

export function norm(v: Float32Array) {
  let s = 0; for (let i = 0; i < v.length; i++) s += v[i] * v[i];
  return Math.sqrt(s);
}

export function normalize(v: Float32Array) {
  const out = new Float32Array(v.length);
  const n = norm(v) || 1;
  for (let i = 0; i < v.length; i++) out[i] = v[i] / n;
  return out;
}

export function add(a: Float32Array, b: Float32Array) {
  const out = new Float32Array(a.length);
  for (let i = 0; i < a.length; i++) out[i] = a[i] + b[i];
  return out;
}

export function scale(a: Float32Array, s: number) {
  const out = new Float32Array(a.length);
  for (let i = 0; i < a.length; i++) out[i] = a[i] * s;
  return out;
}

export function gaussianVector(d: number, rng: () => number) {
  const out = new Float32Array(d);
  for (let i = 0; i < d; i++) out[i] = gaussian(rng);
  return out;
}

// ---------- PCA basis ----------
let BASIS: Float32Array[] = [];

export function loadPCABasisFromFile(filePath?: string) {
  const defaultPath = path.join(process.cwd(), "pca_basis.json");
  const finalPath = filePath || defaultPath;
  
  try {
    const raw = fs.readFileSync(finalPath, "utf-8");
    const obj = JSON.parse(raw);
    const basis = obj.basis as number[][];
    BASIS = basis.map(row => normalize(Float32Array.from(row)));
    if (BASIS.length < 2) throw new Error("Need at least u1,u2 in pca_basis.json");
    console.log(`✅ Loaded PCA basis with ${BASIS.length} components from ${finalPath}`);
    return BASIS;
  } catch (error) {
    console.error(`❌ Failed to load PCA basis from ${finalPath}:`, error);
    throw new Error(`PCA basis file not found or invalid: ${finalPath}`);
  }
}

export function getBasis() {
  if (!BASIS.length) throw new Error("PCA basis not loaded. Call loadPCABasisFromFile() at startup.");
  return BASIS;
}

export function pcaDirectionalBias(theta: number, t: number) {
  const U = getBasis();
  const u1 = U[0], u2 = U[1];
  const alpha = lerp(0.0, 0.35, t);  // tune
  const d = u1.length;
  const dir = new Float32Array(d);
  const c = Math.cos(theta), s = Math.sin(theta);
  for (let i = 0; i < d; i++) dir[i] = c * u1[i] + s * u2[i];
  // normalize and scale by alpha
  const norm = Math.hypot(...dir as unknown as number[]) || 1;
  for (let i = 0; i < d; i++) dir[i] = (dir[i] / norm) * alpha;
  return dir;
}