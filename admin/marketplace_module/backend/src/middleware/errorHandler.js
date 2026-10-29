/**
 * Global Error Handler Middleware
 */
import { logger } from '../utils/logger.js';
import { config } from '../config/index.js';

/**
 * Custom application error class
 */
export class AppError extends Error {
  constructor(message, statusCode = 500, code = 'INTERNAL_ERROR') {
    super(message);
    this.statusCode = statusCode;
    this.code = code;
    this.isOperational = true;
    Error.captureStackTrace(this, this.constructor);
  }
}

/**
 * Common error factory functions
 */
export const errors = {
  badRequest: (message = 'Bad request') => new AppError(message, 400, 'BAD_REQUEST'),
  validation: (message = 'Validation failed') => new AppError(message, 400, 'VALIDATION_ERROR'),
  unauthorized: (message = 'Unauthorized') => new AppError(message, 401, 'UNAUTHORIZED'),
  forbidden: (message = 'Forbidden') => new AppError(message, 403, 'FORBIDDEN'),
  notFound: (message = 'Not found') => new AppError(message, 404, 'NOT_FOUND'),
  conflict: (message = 'Conflict') => new AppError(message, 409, 'CONFLICT'),
  rateLimited: (message = 'Too many requests') => new AppError(message, 429, 'RATE_LIMITED'),
  internal: (message = 'Internal server error') => new AppError(message, 500, 'INTERNAL_ERROR'),
};

/**
 * 404 Not Found handler
 */
export function notFoundHandler(req, res) {
  res.status(404).json({
    success: false,
    error: {
      code: 'NOT_FOUND',
      message: `Route ${req.method} ${req.path} not found`,
    },
  });
}

/**
 * Global error handler middleware
 */
export function errorHandler(err, req, res, _next) {
  // Default error values
  let statusCode = err.statusCode || 500;
  let code = err.code || 'INTERNAL_ERROR';
  let message = err.message || 'An unexpected error occurred';

  // Log the error
  logger.error('Request error', {
    path: req.path,
    method: req.method,
    statusCode,
    code,
    message,
    stack: config.env === 'development' ? err.stack : undefined,
    userId: req.user?.id,
    ip: req.ip,
  });

  // Don't leak internal error details in production
  if (!err.isOperational && config.env === 'production') {
    message = 'An unexpected error occurred';
    code = 'INTERNAL_ERROR';
    statusCode = 500;
  }

  // Send error response
  res.status(statusCode).json({
    success: false,
    error: {
      code,
      message,
      ...(config.env === 'development' && { stack: err.stack }),
    },
  });
}

export default { AppError, errors, notFoundHandler, errorHandler };
