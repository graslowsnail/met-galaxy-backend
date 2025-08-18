import express from "express";
import cors from "cors";
import dotenv from "dotenv";

// Load environment variables
dotenv.config();

const app = express();
const PORT = process.env.PORT || 3000;

// CORS configuration
app.use(
  cors({
    origin:
      process.env.NODE_ENV === "development"
        ? [
            "http://localhost:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3000",
          ]
        : ["https://your-production-domain.com"],
    credentials: true,
    methods: ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allowedHeaders: ["Content-Type", "Authorization", "Cookie"],
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
            environment: process.env.NODE_ENV || 'development'
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
// app.use('/api', yourRoutes)

// Start server
app.listen(PORT, () => {
  console.log(`
    🚀 API Server Started!
    
    📍 Server running on: http://localhost:${PORT}
    🏥 Health Check: http://localhost:${PORT}/health
    🌍 Environment: ${process.env.NODE_ENV || "development"}
  `);
});


