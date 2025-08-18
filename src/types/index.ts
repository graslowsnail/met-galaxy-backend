import { Request } from 'express';

export interface ApiResponse<T = any> {
    success: boolean;
    data?: T;
    error?: string;
    message?: string;
}

// Extend Request interface for custom properties
export interface CustomRequest extends Request {
    // Add custom properties here as needed
}


