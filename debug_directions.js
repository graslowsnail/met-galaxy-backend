#!/usr/bin/env node
/**
 * Direction Debugging Tool for Field Chunk API
 * 
 * This tool helps you understand how chunk coordinates (chunkX, chunkY) 
 * map to semantic directions in your similarity field.
 */

import fs from 'fs';

// Load PCA basis if available
let pcaBasis = null;
try {
  const pcaData = JSON.parse(fs.readFileSync('pca_basis.json', 'utf8'));
  pcaBasis = pcaData.basis;
  console.log(`✅ Loaded PCA basis with ${pcaBasis.length} components`);
} catch (error) {
  console.log(`⚠️  Could not load PCA basis: ${error.message}`);
  console.log(`   Run 'python scripts/pca_build.py' first to generate it.`);
}

// Helper functions (simplified versions from fieldVectors.ts)
function smoothstep(edge0, edge1, x) {
  const t = Math.max(0, Math.min(1, (x - edge0) / Math.max(1e-9, edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

function lerp(a, b, t) {
  return a + (b - a) * t;
}

function radiansToDegrees(rad) {
  return (rad * 180 / Math.PI + 360) % 360;
}

function getCardinalDirection(degrees) {
  if (degrees < 22.5 || degrees >= 337.5) return 'East →';
  if (degrees < 67.5) return 'Northeast ↗';
  if (degrees < 112.5) return 'North ↑';
  if (degrees < 157.5) return 'Northwest ↖';
  if (degrees < 202.5) return 'West ←';
  if (degrees < 247.5) return 'Southwest ↙';
  if (degrees < 292.5) return 'South ↓';
  return 'Southeast ↘';
}

function analyzeChunkPosition(chunkX, chunkY) {
  // Calculate field coordinates (same as in fieldChunk.ts)
  const r = Math.hypot(chunkX, chunkY);
  const theta = Math.atan2(chunkY, chunkX);
  const t = smoothstep(1.5, 12.0, r);
  
  // Calculate bias strength
  const alpha = lerp(0.0, 0.35, t);
  const sigma = lerp(0.05, 0.35, t);
  
  // Convert theta to degrees for readability
  const degrees = radiansToDegrees(theta);
  const cardinal = getCardinalDirection(degrees);
  
  // Calculate pool weights
  const wSim = (1 - t) * (1 - t);
  const wDrift = 2 * t * (1 - t);
  const wRand = t * t;
  const total = wSim + wDrift + wRand;
  
  return {
    chunkX, chunkY, r, theta, t, degrees, cardinal, alpha, sigma,
    weights: {
      sim: wSim / total,
      drift: wDrift / total,  
      rand: wRand / total
    }
  };
}

function describePCADirection(theta, componentIndex = 0) {
  if (!pcaBasis || !pcaBasis[componentIndex]) {
    return "PCA basis not available";
  }
  
  const u1 = pcaBasis[0]; // First PCA component
  const u2 = pcaBasis[1]; // Second PCA component
  
  const c = Math.cos(theta);
  const s = Math.sin(theta);
  
  // This creates the directional bias vector: c * u1 + s * u2
  // The actual semantic meaning depends on what the PCA components represent
  // in your embedding space (which depends on your artwork data)
  
  return `Linear combination: ${c.toFixed(3)} × PC1 + ${s.toFixed(3)} × PC2`;
}

// Test grid of positions
const testPositions = [
  // Center and near-center (high similarity)
  [0, 0],   // Focal point
  [1, 0],   // East
  [0, 1],   // North  
  [-1, 0],  // West
  [0, -1],  // South
  
  // Medium distance (similarity + drift blend)
  [3, 0],   // Far East
  [0, 3],   // Far North
  [-3, 0],  // Far West
  [0, -3],  // Far South
  [2, 2],   // Northeast
  [-2, -2], // Southwest
  
  // Far distance (mostly random + drift)
  [8, 0],   // Very Far East
  [0, 8],   // Very Far North
  [-8, 0],  // Very Far West
  [0, -8],  // Very Far South
  [6, 6],   // Far Northeast
  [-6, -6], // Far Southwest
];

console.log('\n🧭 DIRECTIONAL SIMILARITY FIELD ANALYSIS');
console.log('========================================\n');

console.log('📍 COORDINATE SYSTEM:');
console.log('  • +X = East (right on screen)');
console.log('  • +Y = North (up on screen)');  
console.log('  • -X = West (left on screen)');
console.log('  • -Y = South (down on screen)');
console.log('  • r = distance from focal point');
console.log('  • t = temperature (0=similar, 1=random)\n');

console.log('🎯 SAMPLING POOLS:');
console.log('  • SIM_TIGHT: High similarity to focal artwork');
console.log('  • SIM_DRIFT: Similarity with PCA-based directional bias');  
console.log('  • RAND: Random artworks');
console.log('  • Pool selection is weighted by distance from center\n');

console.log('📊 POSITION ANALYSIS:');
console.log('================================================================================');
console.log('Chunk   | Distance | Angle°  | Direction    | Temp | Sim%  Drift% Rand% | PCA Bias');
console.log('--------|----------|---------|--------------|------|-------|--------|-----|----------');

testPositions.forEach(([x, y]) => {
  const analysis = analyzeChunkPosition(x, y);
  const pcaDesc = describePCADirection(analysis.theta);
  
  console.log(
    `${x.toString().padStart(3)},${y.toString().padStart(3)} | ` +
    `${analysis.r.toFixed(2).padStart(8)} | ` +
    `${analysis.degrees.toFixed(0).padStart(7)} | ` +
    `${analysis.cardinal.padEnd(12)} | ` +
    `${analysis.t.toFixed(2).padStart(4)} | ` +
    `${(analysis.weights.sim * 100).toFixed(0).padStart(4)}  ` +
    `${(analysis.weights.drift * 100).toFixed(0).padStart(5)}  ` +
    `${(analysis.weights.rand * 100).toFixed(0).padStart(4)} | ` +
    `α=${analysis.alpha.toFixed(3)}`
  );
});

console.log('\n🔍 INTERPRETATION:');
console.log('==================');
console.log('• Near center (r < 2): Results are mostly similar to focal artwork');
console.log('• Medium distance (2 < r < 8): Blend of similarity and directional drift'); 
console.log('• Far distance (r > 8): Mostly random with some directional bias');
console.log('• Direction (angle): Uses PCA components to bias similarity in semantic directions');
console.log('• Alpha (α): Strength of directional bias (0 at center, 0.35 max at far distances)');

if (pcaBasis) {
  console.log('\n🧠 PCA COMPONENTS:');
  console.log('==================');
  console.log(`• PC1 (${pcaBasis[0].length}D): First principal component of your artwork embeddings`);
  console.log(`• PC2 (${pcaBasis[0].length}D): Second principal component of your artwork embeddings`);
  console.log('• These capture the main axes of variation in your artwork collection');
  console.log('• The actual semantic meaning depends on your specific artwork data');
  console.log('• Common patterns: style, color, subject matter, artistic period, etc.');
} else {
  console.log('\n⚠️  To see PCA component analysis, run: python scripts/pca_build.py');
}

console.log('\n🎨 PRACTICAL USAGE:');
console.log('===================');
console.log('• Moving East (+X): Explores semantic direction of PC1');
console.log('• Moving North (+Y): Explores semantic direction of PC2'); 
console.log('• Moving diagonally: Explores combinations of PC1 and PC2');
console.log('• The further you go, the more drift and randomness is introduced');
console.log('• Results are deterministic - same coordinates always give same results');

console.log('\n💡 DEBUGGING TIPS:');
console.log('==================');
console.log('• Test with: curl "localhost:8080/api/artworks/field-chunk?targetId=123&chunkX=3&chunkY=0&count=5"');
console.log('• Check response.meta.weights to see pool utilization');
console.log('• Use fixed seed (&seed=42) for reproducible results');
console.log('• Compare results at different distances and angles');