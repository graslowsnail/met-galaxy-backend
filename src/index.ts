import express from "express";
import cors from "cors";
import dotenv from "dotenv";
import { db } from "./db/index.js";
import { sql } from "drizzle-orm";
import artworksRouter from "./routes/artworks.js";
import fieldChunkRouter from "./routes/fieldChunk.js";
import searchRouter from "./routes/search.js";
import likesRouter from "./routes/likes.js";
import { loadPCABasisFromFile } from "./lib/fieldVectors.js";
import { allowedOrigins } from "./lib/allowedOrigins.js";
import { CLIENT_HEADER } from "./middleware/likeGuards.js";

// Load environment variables
dotenv.config();

// Initialize PCA basis at startup
try {
  loadPCABasisFromFile();
} catch (error) {
  console.error("⚠️  Warning: Could not load PCA basis file. Field-chunk endpoint will not work until pca_basis.json is generated.");
  console.error("Run the Python script scripts/pca_build.py to generate the required file.");
}

const app = express();
const PORT = process.env.PORT || 8080;

// Railway fronts the app with two proxy hops, so X-Forwarded-For arrives as
// "<real client>, <railway edge>" and req.ip must resolve to the leftmost entry.
// Verified safe: Railway's edge discards any X-Forwarded-For the caller sends,
// so a client cannot pad the chain to make req.ip resolve to a value it controls.
// If per-IP limits ever start blocking unrelated users, re-check this hop count.
app.set("trust proxy", process.env.NODE_ENV === "development" ? false : 2);

// CORS configuration
app.use(
  cors({
    origin: allowedOrigins,
    credentials: true,
    methods: ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allowedHeaders: ["Content-Type", "Authorization", "Cookie", CLIENT_HEADER],
  })
);

app.use(express.json());

// Health check endpoint
app.get('/health', (req, res) => {
    res.json({
        success: true,
        data: {
            status: 'healthy',
            timestamp: new Date().toISOString(),
            uptime: process.uptime(),
            environment: process.env.NODE_ENV || 'development',
            client: {
                ip: req.ip ?? null,
                forwardedFor: req.get('x-forwarded-for') ?? null
            }
        },
        message: 'Server is running'
    });
});

// Root endpoint
app.get('/', (req, res) => {
    res.json({
        success: true,
        message: 'API Server',
        health: '/health'
    });
});

// API Routes
app.use('/api/artworks', likesRouter);
app.use('/api/artworks', searchRouter);
app.use('/api/artworks', fieldChunkRouter);
app.use('/api/artworks', artworksRouter);

// Test database connection endpoint
app.get('/api/test-db', async (req, res) => {
  const start = Date.now();
  try {
    // Simple query to test connection
    const result = await db.execute(sql`SELECT NOW() as current_time`);
    console.log(`📊 [TEST-DB] Connection successful | ${Date.now() - start}ms`);
    res.json({
      success: true,
      data: result[0],
      message: 'Database connection successful!'
    });
  } catch (error) {
    console.error(`❌ [TEST-DB] Connection failed | ${Date.now() - start}ms:`, error instanceof Error ? error.message : 'Unknown error');
    res.status(500).json({
      success: false,
      error: 'Database connection failed',
      message: error instanceof Error ? error.message : 'Unknown error'
    });
  }
});

// Start server
app.listen(PORT, () => {
  console.log(`
    🚀 [${new Date().toISOString()}] API Server Started!
    
    📍 Server: http://localhost:${PORT}
    🏥 Health: http://localhost:${PORT}/health
    🌍 Environment: ${process.env.NODE_ENV || "development"}
    🎯 Endpoints:
       GET  /api/artworks/random
       GET  /api/artworks/similar/:id
       GET  /api/artworks/field-chunk
       POST /api/artworks/field-chunks
       GET  /api/artworks/search?q=...
  `);
});
