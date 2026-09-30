/**
 * Storage Service - S3-Compatible and Local Filesystem Storage Abstraction
 *
 * Provides unified file storage operations with configurable backend:
 * - Local filesystem (default for development)
 * - S3-compatible storage (SeaweedFS default, AWS S3 supported)
 */
import { S3Client, PutObjectCommand, DeleteObjectCommand, HeadBucketCommand, CreateBucketCommand } from '@aws-sdk/client-s3';
import { query } from '../config/database.js';
import { logger } from '../utils/logger.js';
import fs from 'fs/promises';
import path from 'path';
import { v4 as uuidv4 } from 'uuid';

// Storage configuration cache
let storageConfigCache = null;
let cacheExpiry = 0;
const CACHE_TTL_MS = 60000; // 1 minute

/**
 * Get storage configuration from hub_settings table
 * @returns {Promise<Object>} Storage configuration
 */
export async function getStorageConfig() {
  const now = Date.now();
  if (storageConfigCache && now < cacheExpiry) {
    return storageConfigCache;
  }

  try {
    const result = await query(
      `SELECT setting_key, setting_value FROM hub_settings
       WHERE setting_key LIKE 'storage_%' OR setting_key LIKE 's3_%'`
    );

    const config = {
      storageType: 'local', // Default to local
      localUploadDir: process.env.UPLOAD_DIR || '/tmp/uploads',
      s3Endpoint: process.env.S3_ENDPOINT_URL || 'http://infra-seaweedfs:8333',
      s3Bucket: process.env.S3_BUCKET_NAME || 'waddlebot-assets',
      s3AccessKey: process.env.S3_ACCESS_KEY_ID || '',
      s3SecretKey: process.env.S3_SECRET_ACCESS_KEY || '',
      s3Region: process.env.S3_REGION || 'us-east-1',
      s3PublicUrl: process.env.S3_PUBLIC_BASE_URL || 'http://localhost:8333/waddlebot-assets',
    };

    // Override with database settings
    for (const row of result.rows) {
      switch (row.setting_key) {
        case 'storage_type':
          config.storageType = row.setting_value || 'local';
          break;
        case 's3_endpoint':
          if (row.setting_value) config.s3Endpoint = row.setting_value;
          break;
        case 's3_bucket':
          if (row.setting_value) config.s3Bucket = row.setting_value;
          break;
        case 's3_access_key':
          if (row.setting_value) config.s3AccessKey = row.setting_value;
          break;
        case 's3_secret_key':
          if (row.setting_value) config.s3SecretKey = row.setting_value;
          break;
        case 's3_region':
          if (row.setting_value) config.s3Region = row.setting_value;
          break;
        case 's3_public_url':
          if (row.setting_value) config.s3PublicUrl = row.setting_value;
          break;
      }
    }

    storageConfigCache = config;
    cacheExpiry = now + CACHE_TTL_MS;
    return config;
  } catch (err) {
    logger.error('Failed to get storage config from database', { error: err.message });
    // Return defaults from environment
    return {
      storageType: process.env.S3_STORAGE_ENABLED === 'true' ? 's3' : 'local',
      localUploadDir: process.env.UPLOAD_DIR || '/tmp/uploads',
      s3Endpoint: process.env.S3_ENDPOINT_URL || 'http://infra-seaweedfs:8333',
      s3Bucket: process.env.S3_BUCKET_NAME || 'waddlebot-assets',
      s3AccessKey: process.env.S3_ACCESS_KEY_ID || '',
      s3SecretKey: process.env.S3_SECRET_ACCESS_KEY || '',
      s3Region: process.env.S3_REGION || 'us-east-1',
      s3PublicUrl: process.env.S3_PUBLIC_BASE_URL || 'http://localhost:8333/waddlebot-assets',
    };
  }
}

/**
 * Invalidate storage config cache (call after settings change)
 */
export function invalidateStorageConfigCache() {
  storageConfigCache = null;
  cacheExpiry = 0;
}

/**
 * Check if S3 storage is enabled
 * @returns {Promise<boolean>}
 */
export async function isS3Enabled() {
  const config = await getStorageConfig();
  return config.storageType === 's3';
}

/**
 * Get S3 client instance
 * @returns {Promise<S3Client>}
 */
async function getS3Client() {
  const config = await getStorageConfig();
  return new S3Client({
    endpoint: config.s3Endpoint,
    region: config.s3Region,
    credentials: {
      accessKeyId: config.s3AccessKey,
      secretAccessKey: config.s3SecretKey,
    },
    forcePathStyle: true, // Required for SeaweedFS
  });
}

/**
 * Upload a file to storage
 * @param {Buffer} buffer - File content as buffer
 * @param {string} folder - Storage folder (e.g., 'avatars', 'community-logos')
 * @param {string} filename - Original filename for extension detection
 * @param {string} [contentType] - MIME type
 * @returns {Promise<{url: string, key: string}>} Public URL and storage key
 */
export async function uploadFile(buffer, folder, filename, contentType = 'application/octet-stream') {
  const config = await getStorageConfig();
  const ext = path.extname(filename).toLowerCase();
  const uniqueFilename = `${uuidv4()}${ext}`;
  const key = `${folder}/${uniqueFilename}`;

  if (config.storageType === 's3') {
    return uploadToS3(buffer, key, contentType, config);
  } else {
    return uploadToLocal(buffer, key, config);
  }
}

/**
 * Upload file to S3
 */
async function uploadToS3(buffer, key, contentType, config) {
  try {
    const client = await getS3Client();
    await client.send(new PutObjectCommand({
      Bucket: config.s3Bucket,
      Key: key,
      Body: buffer,
      ContentType: contentType,
      ACL: 'public-read',
    }));

    const url = `${config.s3PublicUrl}/${key}`;
    logger.audit('File uploaded to S3', { key, bucket: config.s3Bucket });
    return { url, key };
  } catch (err) {
    logger.error('S3 upload failed', { error: err.message, key });
    throw new Error(`Failed to upload file to S3: ${err.message}`);
  }
}

/**
 * Upload file to local filesystem
 */
async function uploadToLocal(buffer, key, config) {
  try {
    const fullPath = path.join(config.localUploadDir, key);
    const dir = path.dirname(fullPath);

    // Ensure directory exists
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(fullPath, buffer);

    // Return URL path (served by express static or dedicated route)
    const url = `/uploads/${key}`;
    logger.audit('File uploaded locally', { path: fullPath });
    return { url, key };
  } catch (err) {
    logger.error('Local upload failed', { error: err.message, key });
    throw new Error(`Failed to upload file locally: ${err.message}`);
  }
}

/**
 * Delete a file from storage
 * @param {string} keyOrUrl - Storage key or full URL
 * @returns {Promise<boolean>} Success status
 */
export async function deleteFile(keyOrUrl) {
  const config = await getStorageConfig();

  // Extract key from URL if full URL provided
  let key = keyOrUrl;
  if (keyOrUrl.startsWith('http')) {
    // Extract path from URL
    const url = new URL(keyOrUrl);
    key = url.pathname.replace(`/${config.s3Bucket}/`, '').replace('/uploads/', '');
  } else if (keyOrUrl.startsWith('/uploads/')) {
    key = keyOrUrl.replace('/uploads/', '');
  }

  if (config.storageType === 's3') {
    return deleteFromS3(key, config);
  } else {
    return deleteFromLocal(key, config);
  }
}

/**
 * Delete file from S3
 */
async function deleteFromS3(key, config) {
  try {
    const client = await getS3Client();
    await client.send(new DeleteObjectCommand({
      Bucket: config.s3Bucket,
      Key: key,
    }));
    logger.audit('File deleted from S3', { key, bucket: config.s3Bucket });
    return true;
  } catch (err) {
    logger.error('S3 delete failed', { error: err.message, key });
    return false;
  }
}

/**
 * Delete file from local filesystem
 */
async function deleteFromLocal(key, config) {
  try {
    const fullPath = path.join(config.localUploadDir, key);
    await fs.unlink(fullPath);
    logger.audit('File deleted locally', { path: fullPath });
    return true;
  } catch (err) {
    if (err.code === 'ENOENT') {
      // File doesn't exist, consider it deleted
      return true;
    }
    logger.error('Local delete failed', { error: err.message, key });
    return false;
  }
}

/**
 * Get public URL for a stored file
 * @param {string} key - Storage key
 * @returns {Promise<string>} Public URL
 */
export async function getPublicUrl(key) {
  const config = await getStorageConfig();

  if (!key) return null;

  // If already a full URL, return as-is
  if (key.startsWith('http')) return key;

  // If already a local path, return as-is
  if (key.startsWith('/uploads/')) return key;

  if (config.storageType === 's3') {
    return `${config.s3PublicUrl}/${key}`;
  } else {
    return `/uploads/${key}`;
  }
}

/**
 * Initialize S3 bucket (create if not exists)
 * @returns {Promise<boolean>} Success status
 */
export async function initializeBucket() {
  const config = await getStorageConfig();

  if (config.storageType !== 's3') {
    // Ensure local upload directory exists
    try {
      await fs.mkdir(config.localUploadDir, { recursive: true });
      logger.info('Local upload directory initialized', { path: config.localUploadDir });
      return true;
    } catch (err) {
      logger.error('Failed to create local upload directory', { error: err.message });
      return false;
    }
  }

  try {
    const client = await getS3Client();

    // Check if bucket exists
    try {
      await client.send(new HeadBucketCommand({ Bucket: config.s3Bucket }));
      logger.info('S3 bucket exists', { bucket: config.s3Bucket });
      return true;
    } catch (err) {
      if (err.name === 'NotFound' || err.$metadata?.httpStatusCode === 404) {
        // Bucket doesn't exist, create it
        await client.send(new CreateBucketCommand({ Bucket: config.s3Bucket }));
        logger.info('S3 bucket created', { bucket: config.s3Bucket });
        return true;
      }
      throw err;
    }
  } catch (err) {
    logger.error('Failed to initialize S3 bucket', { error: err.message, bucket: config.s3Bucket });
    return false;
  }
}

/**
 * Test S3 connection
 * @param {Object} testConfig - Configuration to test
 * @returns {Promise<{success: boolean, message: string}>}
 */
export async function testS3Connection(testConfig) {
  try {
    const client = new S3Client({
      endpoint: testConfig.s3Endpoint,
      region: testConfig.s3Region || 'us-east-1',
      credentials: {
        accessKeyId: testConfig.s3AccessKey,
        secretAccessKey: testConfig.s3SecretKey,
      },
      forcePathStyle: true,
    });

    await client.send(new HeadBucketCommand({ Bucket: testConfig.s3Bucket }));
    return { success: true, message: 'Connection successful' };
  } catch (err) {
    if (err.name === 'NotFound' || err.$metadata?.httpStatusCode === 404) {
      return { success: true, message: 'Connection successful (bucket does not exist yet)' };
    }
    return { success: false, message: err.message };
  }
}

/**
 * Get allowed MIME types for images
 */
export const ALLOWED_IMAGE_TYPES = [
  'image/jpeg',
  'image/png',
  'image/gif',
  'image/webp',
];

/**
 * Validate file is an allowed image type
 * @param {string} mimeType - MIME type to check
 * @returns {boolean}
 */
export function isAllowedImageType(mimeType) {
  return ALLOWED_IMAGE_TYPES.includes(mimeType);
}

/**
 * Max file sizes (in bytes)
 */
export const MAX_FILE_SIZES = {
  avatar: 5 * 1024 * 1024, // 5MB
  banner: 10 * 1024 * 1024, // 10MB
  logo: 5 * 1024 * 1024, // 5MB
};

export default {
  getStorageConfig,
  invalidateStorageConfigCache,
  isS3Enabled,
  uploadFile,
  deleteFile,
  getPublicUrl,
  initializeBucket,
  testS3Connection,
  isAllowedImageType,
  ALLOWED_IMAGE_TYPES,
  MAX_FILE_SIZES,
};
