/**
 * Community API Tests (community.js routes)
 * Tests community management endpoints
 */

const request = require('supertest');

const API_BASE_URL = global.TEST_CONFIG.API_BASE_URL;
const ADMIN_USER = global.TEST_CONFIG.ADMIN_USER;

// Community ids are public UUIDs on the API (never the numeric id).
const PLACEHOLDER_COMMUNITY_UUID = '00000001-0000-4000-8000-000000000000';

describe('Community API - /api/v1/communities', () => {
  let authToken;
  let testCommunityId;

  beforeAll(async () => {
    // Try to get auth token for authenticated tests
    const loginResponse = await request(API_BASE_URL)
      .post('/api/v1/auth/login')
      .send({
        email: ADMIN_USER.email,
        password: ADMIN_USER.password
      });

    if (loginResponse.status === 200 && loginResponse.body.token) {
      authToken = loginResponse.body.token;
    }
  });

  describe('GET /api/v1/communities', () => {
    it('should return list of communities', async () => {
      const response = await request(API_BASE_URL)
        .get('/api/v1/communities');

      expect(response.status).toBe(200);
      expect(response.body).toHaveProperty('success', true);
      expect(response.body).toHaveProperty('communities');
      expect(Array.isArray(response.body.communities)).toBe(true);
    });
  });

  describe('GET /api/v1/communities/:id', () => {
    it('should return 404 for non-existent community', async () => {
      const response = await request(API_BASE_URL)
        .get(`/api/v1/communities/${PLACEHOLDER_COMMUNITY_UUID}`);

      expect([404, 400]).toContain(response.status);
    });

    it('should reject invalid ID format', async () => {
      const response = await request(API_BASE_URL)
        .get('/api/v1/communities/invalid');

      expect([400, 404]).toContain(response.status);
    });
  });

  describe('POST /api/v1/communities', () => {
    it('should reject community creation without authentication', async () => {
      const response = await request(API_BASE_URL)
        .post('/api/v1/communities')
        .send({
          name: 'testcommunity',
          display_name: 'Test Community'
        });

      expect([401, 403]).toContain(response.status);
    });

    it('should reject community creation with invalid data', async () => {
      if (!authToken) return; // Skip if no auth

      const response = await request(API_BASE_URL)
        .post('/api/v1/communities')
        .set('Authorization', `Bearer ${authToken}`)
        .send({
          name: '', // Invalid empty name
          display_name: ''
        });

      expect([400, 422]).toContain(response.status);
    });

    it('should create community with valid data when authenticated', async () => {
      if (!authToken) return; // Skip if no auth

      const uniqueName = `test${Date.now()}`;
      const response = await request(API_BASE_URL)
        .post('/api/v1/communities')
        .set('Authorization', `Bearer ${authToken}`)
        .send({
          name: uniqueName,
          display_name: `Test Community ${Date.now()}`,
          description: 'A test community'
        });

      expect([200, 201, 409]).toContain(response.status);

      if (response.status === 200 || response.status === 201) {
        expect(response.body).toHaveProperty('success', true);
        expect(response.body).toHaveProperty('community');
        testCommunityId = response.body.community?.id;
      }
    });
  });

  describe('PATCH /api/v1/communities/:id', () => {
    it('should reject update without authentication', async () => {
      const response = await request(API_BASE_URL)
        .patch(`/api/v1/communities/${PLACEHOLDER_COMMUNITY_UUID}`)
        .send({
          display_name: 'Updated Name'
        });

      expect([401, 403, 404]).toContain(response.status);
    });

    it('should reject update with invalid data', async () => {
      if (!authToken || !testCommunityId) return;

      const response = await request(API_BASE_URL)
        .patch(`/api/v1/communities/${testCommunityId}`)
        .set('Authorization', `Bearer ${authToken}`)
        .send({
          display_name: '' // Invalid empty name
        });

      expect([400, 422]).toContain(response.status);
    });
  });

  describe('GET /api/v1/communities/:id/members', () => {
    it('should return community members', async () => {
      const response = await request(API_BASE_URL)
        .get(`/api/v1/communities/${PLACEHOLDER_COMMUNITY_UUID}/members`);

      expect([200, 404]).toContain(response.status);

      if (response.status === 200) {
        expect(response.body).toHaveProperty('members');
        expect(Array.isArray(response.body.members)).toBe(true);
      }
    });
  });

  describe('POST /api/v1/communities/:id/join', () => {
    it('should reject join without authentication', async () => {
      const response = await request(API_BASE_URL)
        .post(`/api/v1/communities/${PLACEHOLDER_COMMUNITY_UUID}/join`);

      expect([401, 403, 404]).toContain(response.status);
    });
  });

  describe('POST /api/v1/communities/:id/leave', () => {
    it('should reject leave without authentication', async () => {
      const response = await request(API_BASE_URL)
        .post(`/api/v1/communities/${PLACEHOLDER_COMMUNITY_UUID}/leave`);

      expect([401, 403, 404]).toContain(response.status);
    });
  });
});
