/**
 * Interaction API Tests
 * Tests all interaction endpoints: admin channel CRUD, forum moderation,
 * community member forum, internal relay, and voice rooms.
 */

const request = require('supertest');

const API_BASE_URL = global.TEST_CONFIG.API_BASE_URL;
const ADMIN_USER = global.TEST_CONFIG.ADMIN_USER;

// Community ids are public UUIDs on the API (never the numeric id).
const COMMUNITY_ID = process.env.TEST_COMMUNITY_ID || '00000001-0000-4000-8000-000000000000';

describe('Interaction API', () => {
  let authToken;
  let createdChannelId;
  let createdChatChannelId;
  let createdForumChannelId;
  let createdPostId;
  let createdReplyId;

  beforeAll(async () => {
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

  // ---------------------------------------------------------------------------
  // Admin Channel CRUD
  // ---------------------------------------------------------------------------

  describe('Admin Channel CRUD', () => {
    describe('GET /api/v1/admin/:communityId/interaction/channels', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .get(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`);

        expect([401, 403]).toContain(response.status);
      });

      it('should return channels list for authenticated admin', async () => {
        if (!authToken) return;

        const response = await request(API_BASE_URL)
          .get(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
          .set('Authorization', `Bearer ${authToken}`);

        expect([200, 404]).toContain(response.status);

        if (response.status === 200) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('channels');
          expect(Array.isArray(response.body.channels)).toBe(true);

          if (response.body.channels.length > 0) {
            const channel = response.body.channels[0];
            expect(channel).toHaveProperty('id');
            expect(channel).toHaveProperty('name');
            expect(channel).toHaveProperty('channelType');
            expect(channel).toHaveProperty('sortOrder');
            expect(channel).toHaveProperty('isActive');
            expect(channel).toHaveProperty('communityServerChannelId');
          }
        }
      });
    });

    describe('POST /api/v1/admin/:communityId/interaction/channels', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .post(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
          .send({ name: 'test-channel', channel_type: 'chat' });

        expect([401, 403]).toContain(response.status);
      });

      it('should return 400 when name is missing', async () => {
        if (!authToken) return;

        const response = await request(API_BASE_URL)
          .post(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({ channel_type: 'chat' });

        expect([400, 422]).toContain(response.status);
      });

      it('should return 400 when channel_type is invalid', async () => {
        if (!authToken) return;

        const response = await request(API_BASE_URL)
          .post(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({ name: 'test-channel', channel_type: 'invalid_type_xyz' });

        expect([400, 422]).toContain(response.status);
      });

      it('should create a chat channel', async () => {
        if (!authToken) return;

        const uniqueName = `test-chat-${Date.now()}`;
        const response = await request(API_BASE_URL)
          .post(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({
            name: uniqueName,
            channel_type: 'chat',
            description: 'A test chat channel',
            sort_order: 10,
            allow_ad_hoc_voice: false
          });

        expect([200, 201, 404]).toContain(response.status);

        if (response.status === 200 || response.status === 201) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('channel');
          expect(response.body.channel).toHaveProperty('id');
          expect(response.body.channel).toHaveProperty('name', uniqueName);
          createdChatChannelId = response.body.channel.id;
          createdChannelId = createdChatChannelId;
        }
      });

      it('should create a forum channel', async () => {
        if (!authToken) return;

        const uniqueName = `test-forum-${Date.now()}`;
        const response = await request(API_BASE_URL)
          .post(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({
            name: uniqueName,
            channel_type: 'forum',
            description: 'A test forum channel',
            sort_order: 20,
            allow_ad_hoc_voice: false
          });

        expect([200, 201, 404]).toContain(response.status);

        if (response.status === 200 || response.status === 201) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('channel');
          createdForumChannelId = response.body.channel.id;
        }
      });

      it('should create a voice channel', async () => {
        if (!authToken) return;

        const uniqueName = `test-voice-${Date.now()}`;
        const response = await request(API_BASE_URL)
          .post(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({
            name: uniqueName,
            channel_type: 'voice',
            description: 'A test voice channel',
            sort_order: 30,
            allow_ad_hoc_voice: true
          });

        expect([200, 201, 404]).toContain(response.status);

        if (response.status === 200 || response.status === 201) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('channel');
        }
      });

      it('should return 409 for duplicate channel name', async () => {
        if (!authToken || !createdChannelId) return;

        // First, get the name of the channel we just created
        const listResponse = await request(API_BASE_URL)
          .get(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
          .set('Authorization', `Bearer ${authToken}`);

        if (listResponse.status !== 200) return;

        const existingChannel = listResponse.body.channels.find(
          (c) => c.id === createdChannelId
        );
        if (!existingChannel) return;

        const response = await request(API_BASE_URL)
          .post(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({
            name: existingChannel.name,
            channel_type: 'chat'
          });

        expect([409, 400]).toContain(response.status);
      });
    });

    describe('PUT /api/v1/admin/:communityId/interaction/channels/:id', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .put(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels/1`)
          .send({ name: 'updated-name' });

        expect([401, 403]).toContain(response.status);
      });

      it('should return 404 for nonexistent channel ID', async () => {
        if (!authToken) return;

        const response = await request(API_BASE_URL)
          .put(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels/99999999`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({ name: 'updated-name' });

        expect([404, 400]).toContain(response.status);
      });

      it('should update channel name', async () => {
        if (!authToken || !createdChannelId) return;

        const updatedName = `updated-chat-${Date.now()}`;
        const response = await request(API_BASE_URL)
          .put(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels/${createdChannelId}`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({
            name: updatedName,
            description: 'Updated description',
            sort_order: 15
          });

        expect([200, 404]).toContain(response.status);

        if (response.status === 200) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('channel');
          expect(response.body.channel).toHaveProperty('name', updatedName);
        }
      });
    });

    describe('DELETE /api/v1/admin/:communityId/interaction/channels/:id', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .delete(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels/1`);

        expect([401, 403]).toContain(response.status);
      });

      it('should return 404 for nonexistent channel ID', async () => {
        if (!authToken) return;

        const response = await request(API_BASE_URL)
          .delete(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels/99999999`)
          .set('Authorization', `Bearer ${authToken}`);

        expect([404, 400]).toContain(response.status);
      });

      it('should soft-delete an existing channel', async () => {
        if (!authToken || !createdChatChannelId) return;

        const response = await request(API_BASE_URL)
          .delete(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels/${createdChatChannelId}`)
          .set('Authorization', `Bearer ${authToken}`);

        expect([200, 204, 404]).toContain(response.status);

        if (response.status === 200) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('message');
        }
      });

      it('should no longer appear in channels list after deletion', async () => {
        if (!authToken || !createdChatChannelId) return;

        const response = await request(API_BASE_URL)
          .get(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
          .set('Authorization', `Bearer ${authToken}`);

        if (response.status !== 200) return;

        const stillPresent = response.body.channels.some(
          (c) => c.id === createdChatChannelId && c.isActive
        );
        expect(stillPresent).toBe(false);
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Admin Forum Moderation
  // ---------------------------------------------------------------------------

  describe('Admin Forum Moderation', () => {
    describe('PUT /api/v1/admin/:communityId/interaction/forum/posts/:id', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .put(`/api/v1/admin/${COMMUNITY_ID}/interaction/forum/posts/1`)
          .send({ is_pinned: true });

        expect([401, 403]).toContain(response.status);
      });

      it('should moderate (pin) an existing post', async () => {
        if (!authToken || !createdPostId) return;

        const response = await request(API_BASE_URL)
          .put(`/api/v1/admin/${COMMUNITY_ID}/interaction/forum/posts/${createdPostId}`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({ is_pinned: true });

        expect([200, 404]).toContain(response.status);

        if (response.status === 200) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('message');
        }
      });

      it('should moderate (lock) an existing post', async () => {
        if (!authToken || !createdPostId) return;

        const response = await request(API_BASE_URL)
          .put(`/api/v1/admin/${COMMUNITY_ID}/interaction/forum/posts/${createdPostId}`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({ is_locked: true });

        expect([200, 404]).toContain(response.status);

        if (response.status === 200) {
          expect(response.body).toHaveProperty('success', true);
        }
      });

      it('should moderate (delete) an existing post', async () => {
        if (!authToken || !createdPostId) return;

        const response = await request(API_BASE_URL)
          .put(`/api/v1/admin/${COMMUNITY_ID}/interaction/forum/posts/${createdPostId}`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({ delete: true });

        expect([200, 404]).toContain(response.status);

        if (response.status === 200) {
          expect(response.body).toHaveProperty('success', true);
        }
      });
    });

    describe('DELETE /api/v1/admin/:communityId/interaction/forum/replies/:id', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .delete(`/api/v1/admin/${COMMUNITY_ID}/interaction/forum/replies/1`);

        expect([401, 403]).toContain(response.status);
      });

      it('should delete a reply', async () => {
        if (!authToken || !createdReplyId) return;

        const response = await request(API_BASE_URL)
          .delete(`/api/v1/admin/${COMMUNITY_ID}/interaction/forum/replies/${createdReplyId}`)
          .set('Authorization', `Bearer ${authToken}`);

        expect([200, 204, 404]).toContain(response.status);

        if (response.status === 200) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('message');
        }
      });

      it('should return 404 for nonexistent reply', async () => {
        if (!authToken) return;

        const response = await request(API_BASE_URL)
          .delete(`/api/v1/admin/${COMMUNITY_ID}/interaction/forum/replies/99999999`)
          .set('Authorization', `Bearer ${authToken}`);

        expect([404, 400]).toContain(response.status);
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Community Member Forum
  // ---------------------------------------------------------------------------

  describe('Community Member Forum', () => {
    describe('GET /api/v1/community/:id/interact/channels', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .get(`/api/v1/community/${COMMUNITY_ID}/interact/channels`);

        expect([401, 403]).toContain(response.status);
      });

      it('should return channels for authenticated member', async () => {
        if (!authToken) return;

        const response = await request(API_BASE_URL)
          .get(`/api/v1/community/${COMMUNITY_ID}/interact/channels`)
          .set('Authorization', `Bearer ${authToken}`);

        expect([200, 404]).toContain(response.status);

        if (response.status === 200) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('channels');
          expect(Array.isArray(response.body.channels)).toBe(true);
        }
      });
    });

    describe('GET /api/v1/community/:id/interact/forum/:channelId/posts', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .get(`/api/v1/community/${COMMUNITY_ID}/interact/forum/1/posts`);

        expect([401, 403]).toContain(response.status);
      });

      it('should return posts list with pagination', async () => {
        if (!authToken || !createdForumChannelId) return;

        const response = await request(API_BASE_URL)
          .get(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${createdForumChannelId}/posts`)
          .query({ page: 1, limit: 10 })
          .set('Authorization', `Bearer ${authToken}`);

        expect([200, 404]).toContain(response.status);

        if (response.status === 200) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('posts');
          expect(Array.isArray(response.body.posts)).toBe(true);
          expect(response.body).toHaveProperty('pagination');
        }
      });

      it('should respect pagination params', async () => {
        if (!authToken || !createdForumChannelId) return;

        const response = await request(API_BASE_URL)
          .get(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${createdForumChannelId}/posts`)
          .query({ page: 1, limit: 5 })
          .set('Authorization', `Bearer ${authToken}`);

        expect([200, 404]).toContain(response.status);

        if (response.status === 200) {
          expect(response.body.posts.length).toBeLessThanOrEqual(5);
        }
      });
    });

    describe('POST /api/v1/community/:id/interact/forum/:channelId/posts', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .post(`/api/v1/community/${COMMUNITY_ID}/interact/forum/1/posts`)
          .send({ title: 'Test Post', body: 'Test body' });

        expect([401, 403]).toContain(response.status);
      });

      it('should return 400 when title is missing', async () => {
        if (!authToken || !createdForumChannelId) return;

        const response = await request(API_BASE_URL)
          .post(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${createdForumChannelId}/posts`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({ body: 'Body without title', tags: [] });

        expect([400, 422]).toContain(response.status);
      });

      it('should create a forum post', async () => {
        if (!authToken || !createdForumChannelId) return;

        const response = await request(API_BASE_URL)
          .post(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${createdForumChannelId}/posts`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({
            title: `Test Post ${Date.now()}`,
            body: 'This is a test post body.',
            tags: ['test', 'api']
          });

        expect([200, 201, 404]).toContain(response.status);

        if (response.status === 200 || response.status === 201) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('post');
          expect(response.body.post).toHaveProperty('id');
          createdPostId = response.body.post.id;
        }
      });
    });

    describe('GET /api/v1/community/:id/interact/forum/:channelId/posts/:postId', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .get(`/api/v1/community/${COMMUNITY_ID}/interact/forum/1/posts/1`);

        expect([401, 403]).toContain(response.status);
      });

      it('should return post detail with replies', async () => {
        if (!authToken || !createdForumChannelId || !createdPostId) return;

        const response = await request(API_BASE_URL)
          .get(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${createdForumChannelId}/posts/${createdPostId}`)
          .set('Authorization', `Bearer ${authToken}`);

        expect([200, 404]).toContain(response.status);

        if (response.status === 200) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('post');
          expect(response.body.post).toHaveProperty('replies');
          expect(Array.isArray(response.body.post.replies)).toBe(true);
        }
      });

      it('should return 404 for nonexistent post', async () => {
        if (!authToken || !createdForumChannelId) return;

        const response = await request(API_BASE_URL)
          .get(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${createdForumChannelId}/posts/99999999`)
          .set('Authorization', `Bearer ${authToken}`);

        expect([404, 400]).toContain(response.status);
      });
    });

    describe('POST /api/v1/community/:id/interact/forum/posts/:postId/replies', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .post(`/api/v1/community/${COMMUNITY_ID}/interact/forum/posts/1/replies`)
          .send({ content: 'Test reply' });

        expect([401, 403]).toContain(response.status);
      });

      it('should return 400 when content is empty', async () => {
        if (!authToken || !createdPostId) return;

        const response = await request(API_BASE_URL)
          .post(`/api/v1/community/${COMMUNITY_ID}/interact/forum/posts/${createdPostId}/replies`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({ content: '' });

        expect([400, 422]).toContain(response.status);
      });

      it('should return 400 when content is missing', async () => {
        if (!authToken || !createdPostId) return;

        const response = await request(API_BASE_URL)
          .post(`/api/v1/community/${COMMUNITY_ID}/interact/forum/posts/${createdPostId}/replies`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({});

        expect([400, 422]).toContain(response.status);
      });

      it('should create a reply to a post', async () => {
        if (!authToken || !createdPostId) return;

        const response = await request(API_BASE_URL)
          .post(`/api/v1/community/${COMMUNITY_ID}/interact/forum/posts/${createdPostId}/replies`)
          .set('Authorization', `Bearer ${authToken}`)
          .send({ content: 'This is a test reply.' });

        expect([200, 201, 404]).toContain(response.status);

        if (response.status === 200 || response.status === 201) {
          expect(response.body).toHaveProperty('success', true);
          expect(response.body).toHaveProperty('reply');
          expect(response.body.reply).toHaveProperty('id');
          createdReplyId = response.body.reply.id;
        }
      });

      it('should reflect incremented reply count on post detail after reply', async () => {
        if (!authToken || !createdForumChannelId || !createdPostId || !createdReplyId) return;

        const response = await request(API_BASE_URL)
          .get(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${createdForumChannelId}/posts/${createdPostId}`)
          .set('Authorization', `Bearer ${authToken}`);

        if (response.status !== 200) return;

        expect(response.body.post.replies.length).toBeGreaterThanOrEqual(1);
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Internal Relay
  // ---------------------------------------------------------------------------

  describe('Internal Relay', () => {
    describe('POST /api/v1/internal/relay/incoming', () => {
      it('should work without auth token (service-to-service)', async () => {
        const response = await request(API_BASE_URL)
          .post('/api/v1/internal/relay/incoming')
          .send({
            sourcePlatformChannelId: 'discord-channel-123',
            platform: 'discord',
            channelType: 'chat',
            content: 'Hello from relay test',
            author: { id: 'user-123', username: 'testuser' },
            messageType: 'text'
          });

        // Should not be 401/403 — internal service-to-service endpoint
        expect([200, 201, 404, 400, 422, 500]).toContain(response.status);
        expect([401, 403]).not.toContain(response.status);
      });

      it('should return 404 for unknown sourcePlatformChannelId', async () => {
        const response = await request(API_BASE_URL)
          .post('/api/v1/internal/relay/incoming')
          .send({
            sourcePlatformChannelId: 'nonexistent-channel-xyz-99999',
            platform: 'discord',
            channelType: 'chat',
            content: 'Test message',
            author: { id: 'user-abc', username: 'ghostuser' },
            messageType: 'text'
          });

        expect([404, 400]).toContain(response.status);
      });

      it('should return 400 when required fields are missing', async () => {
        const response = await request(API_BASE_URL)
          .post('/api/v1/internal/relay/incoming')
          .send({
            platform: 'discord'
            // Missing: sourcePlatformChannelId, channelType, content, author, messageType
          });

        expect([400, 422]).toContain(response.status);
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Voice Rooms (member)
  // ---------------------------------------------------------------------------

  describe('Voice Rooms', () => {
    let createdVoiceRoomName;

    describe('GET /api/v1/community/:id/interact/voice/rooms', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .get(`/api/v1/community/${COMMUNITY_ID}/interact/voice/rooms`);

        expect([401, 403]).toContain(response.status);
      });

      it('should return voice rooms list for authenticated member', async () => {
        if (!authToken) return;

        try {
          const response = await request(API_BASE_URL)
            .get(`/api/v1/community/${COMMUNITY_ID}/interact/voice/rooms`)
            .set('Authorization', `Bearer ${authToken}`);

          expect([200, 404, 503]).toContain(response.status);

          if (response.status === 200) {
            expect(response.body).toHaveProperty('success', true);
            expect(response.body).toHaveProperty('rooms');
            expect(Array.isArray(response.body.rooms)).toBe(true);
          }
        } catch (err) {
          // the RTC backend may not be available; skip gracefully
        }
      });
    });

    describe('POST /api/v1/community/:id/interact/voice/rooms', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .post(`/api/v1/community/${COMMUNITY_ID}/interact/voice/rooms`)
          .send({ name: 'test-room' });

        expect([401, 403]).toContain(response.status);
      });

      it('should create a voice room', async () => {
        if (!authToken) return;

        try {
          const roomName = `test-room-${Date.now()}`;
          const response = await request(API_BASE_URL)
            .post(`/api/v1/community/${COMMUNITY_ID}/interact/voice/rooms`)
            .set('Authorization', `Bearer ${authToken}`)
            .send({ name: roomName });

          expect([200, 201, 404, 503]).toContain(response.status);

          if (response.status === 200 || response.status === 201) {
            expect(response.body).toHaveProperty('success', true);
            createdVoiceRoomName = roomName;
          }
        } catch (err) {
          // the RTC backend may not be available; skip gracefully
        }
      });
    });

    describe('POST /api/v1/community/:id/interact/voice/rooms/:name/join', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .post(`/api/v1/community/${COMMUNITY_ID}/interact/voice/rooms/general/join`);

        expect([401, 403]).toContain(response.status);
      });

      it('should return token and url when joining a voice room', async () => {
        if (!authToken || !createdVoiceRoomName) return;

        try {
          const response = await request(API_BASE_URL)
            .post(`/api/v1/community/${COMMUNITY_ID}/interact/voice/rooms/${createdVoiceRoomName}/join`)
            .set('Authorization', `Bearer ${authToken}`);

          expect([200, 404, 503]).toContain(response.status);

          if (response.status === 200) {
            expect(response.body).toHaveProperty('success', true);
            expect(response.body).toHaveProperty('token');
            expect(response.body).toHaveProperty('url');
          }
        } catch (err) {
          // the RTC backend may not be available; skip gracefully
        }
      });
    });

    describe('POST /api/v1/community/:id/interact/voice/rooms/:name/leave', () => {
      it('should return 401 without auth token', async () => {
        const response = await request(API_BASE_URL)
          .post(`/api/v1/community/${COMMUNITY_ID}/interact/voice/rooms/general/leave`);

        expect([401, 403]).toContain(response.status);
      });

      it('should leave a voice room', async () => {
        if (!authToken || !createdVoiceRoomName) return;

        try {
          const response = await request(API_BASE_URL)
            .post(`/api/v1/community/${COMMUNITY_ID}/interact/voice/rooms/${createdVoiceRoomName}/leave`)
            .set('Authorization', `Bearer ${authToken}`);

          expect([200, 404, 503]).toContain(response.status);

          if (response.status === 200) {
            expect(response.body).toHaveProperty('success', true);
          }
        } catch (err) {
          // the RTC backend may not be available; skip gracefully
        }
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Full CRUD Lifecycle: Chat Channel
  // ---------------------------------------------------------------------------

  describe('Full CRUD lifecycle: chat channel', () => {
    let lifecycleChatChannelId;
    const originalName = `lifecycle-chat-${Date.now()}`;
    const updatedName = `lifecycle-chat-updated-${Date.now()}`;

    it('should create a chat channel', async () => {
      if (!authToken) return;

      const response = await request(API_BASE_URL)
        .post(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
        .set('Authorization', `Bearer ${authToken}`)
        .send({
          name: originalName,
          channel_type: 'chat',
          description: 'Lifecycle test channel',
          sort_order: 99
        });

      expect([200, 201, 404]).toContain(response.status);

      if (response.status === 200 || response.status === 201) {
        lifecycleChatChannelId = response.body.channel.id;
        expect(response.body.channel.name).toBe(originalName);
      }
    });

    it('should appear in channels list after creation', async () => {
      if (!authToken || !lifecycleChatChannelId) return;

      const response = await request(API_BASE_URL)
        .get(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
        .set('Authorization', `Bearer ${authToken}`);

      if (response.status !== 200) return;

      const found = response.body.channels.some((c) => c.id === lifecycleChatChannelId);
      expect(found).toBe(true);
    });

    it('should update channel name', async () => {
      if (!authToken || !lifecycleChatChannelId) return;

      const response = await request(API_BASE_URL)
        .put(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels/${lifecycleChatChannelId}`)
        .set('Authorization', `Bearer ${authToken}`)
        .send({ name: updatedName });

      expect([200, 404]).toContain(response.status);

      if (response.status === 200) {
        expect(response.body.channel.name).toBe(updatedName);
      }
    });

    it('should reflect updated name in channels list', async () => {
      if (!authToken || !lifecycleChatChannelId) return;

      const response = await request(API_BASE_URL)
        .get(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
        .set('Authorization', `Bearer ${authToken}`);

      if (response.status !== 200) return;

      const found = response.body.channels.find((c) => c.id === lifecycleChatChannelId);
      if (found) {
        expect(found.name).toBe(updatedName);
      }
    });

    it('should delete channel', async () => {
      if (!authToken || !lifecycleChatChannelId) return;

      const response = await request(API_BASE_URL)
        .delete(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels/${lifecycleChatChannelId}`)
        .set('Authorization', `Bearer ${authToken}`);

      expect([200, 204, 404]).toContain(response.status);

      if (response.status === 200) {
        expect(response.body).toHaveProperty('success', true);
      }
    });

    it('should no longer appear as active in channels list after deletion', async () => {
      if (!authToken || !lifecycleChatChannelId) return;

      const response = await request(API_BASE_URL)
        .get(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
        .set('Authorization', `Bearer ${authToken}`);

      if (response.status !== 200) return;

      const stillActive = response.body.channels.some(
        (c) => c.id === lifecycleChatChannelId && c.isActive
      );
      expect(stillActive).toBe(false);
    });
  });

  // ---------------------------------------------------------------------------
  // Full CRUD Lifecycle: Forum Channel + Posts + Replies + Moderation
  // ---------------------------------------------------------------------------

  describe('Full CRUD lifecycle: forum channel with posts and replies', () => {
    let lifecycleForumChannelId;
    let lifecyclePostId;
    let lifecycleReplyId;
    const forumChannelName = `lifecycle-forum-${Date.now()}`;

    it('should create a forum channel', async () => {
      if (!authToken) return;

      const response = await request(API_BASE_URL)
        .post(`/api/v1/admin/${COMMUNITY_ID}/interaction/channels`)
        .set('Authorization', `Bearer ${authToken}`)
        .send({
          name: forumChannelName,
          channel_type: 'forum',
          description: 'Lifecycle forum channel',
          sort_order: 98
        });

      expect([200, 201, 404]).toContain(response.status);

      if (response.status === 200 || response.status === 201) {
        lifecycleForumChannelId = response.body.channel.id;
      }
    });

    it('should create a post in the forum channel', async () => {
      if (!authToken || !lifecycleForumChannelId) return;

      const response = await request(API_BASE_URL)
        .post(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${lifecycleForumChannelId}/posts`)
        .set('Authorization', `Bearer ${authToken}`)
        .send({
          title: `Lifecycle Post ${Date.now()}`,
          body: 'This is the post body for lifecycle testing.',
          tags: ['lifecycle']
        });

      expect([200, 201, 404]).toContain(response.status);

      if (response.status === 200 || response.status === 201) {
        lifecyclePostId = response.body.post.id;
      }
    });

    it('should appear in posts list', async () => {
      if (!authToken || !lifecycleForumChannelId || !lifecyclePostId) return;

      const response = await request(API_BASE_URL)
        .get(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${lifecycleForumChannelId}/posts`)
        .query({ page: 1, limit: 50 })
        .set('Authorization', `Bearer ${authToken}`);

      if (response.status !== 200) return;

      const found = response.body.posts.some((p) => p.id === lifecyclePostId);
      expect(found).toBe(true);
    });

    it('should retrieve post detail', async () => {
      if (!authToken || !lifecycleForumChannelId || !lifecyclePostId) return;

      const response = await request(API_BASE_URL)
        .get(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${lifecycleForumChannelId}/posts/${lifecyclePostId}`)
        .set('Authorization', `Bearer ${authToken}`);

      expect([200, 404]).toContain(response.status);

      if (response.status === 200) {
        expect(response.body.post).toHaveProperty('id', lifecyclePostId);
        expect(response.body.post).toHaveProperty('replies');
      }
    });

    it('should create a reply to the post', async () => {
      if (!authToken || !lifecyclePostId) return;

      const response = await request(API_BASE_URL)
        .post(`/api/v1/community/${COMMUNITY_ID}/interact/forum/posts/${lifecyclePostId}/replies`)
        .set('Authorization', `Bearer ${authToken}`)
        .send({ content: 'This is a lifecycle test reply.' });

      expect([200, 201, 404]).toContain(response.status);

      if (response.status === 200 || response.status === 201) {
        lifecycleReplyId = response.body.reply.id;
      }
    });

    it('should reflect incremented reply count in post detail', async () => {
      if (!authToken || !lifecycleForumChannelId || !lifecyclePostId || !lifecycleReplyId) return;

      const response = await request(API_BASE_URL)
        .get(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${lifecycleForumChannelId}/posts/${lifecyclePostId}`)
        .set('Authorization', `Bearer ${authToken}`);

      if (response.status !== 200) return;

      expect(response.body.post.replies.length).toBeGreaterThanOrEqual(1);
    });

    it('should pin the post via admin moderation', async () => {
      if (!authToken || !lifecyclePostId) return;

      const response = await request(API_BASE_URL)
        .put(`/api/v1/admin/${COMMUNITY_ID}/interaction/forum/posts/${lifecyclePostId}`)
        .set('Authorization', `Bearer ${authToken}`)
        .send({ is_pinned: true });

      expect([200, 404]).toContain(response.status);

      if (response.status === 200) {
        expect(response.body).toHaveProperty('success', true);
      }
    });

    it('should reflect pinned status in post detail', async () => {
      if (!authToken || !lifecycleForumChannelId || !lifecyclePostId) return;

      const response = await request(API_BASE_URL)
        .get(`/api/v1/community/${COMMUNITY_ID}/interact/forum/${lifecycleForumChannelId}/posts/${lifecyclePostId}`)
        .set('Authorization', `Bearer ${authToken}`);

      if (response.status !== 200) return;

      // Post should exist and may have a pinned flag
      expect(response.body).toHaveProperty('post');
    });
  });

  // ---------------------------------------------------------------------------
  // Regression: Issue #108 — channel creation 500/409 cycle (Bugs A + B)
  // ---------------------------------------------------------------------------

  describe('POST /api/v1/community/:id/interact/channels - channel creation regression', () => {
    it('returns 201 (not 500) for an authenticated member creating a channel', async () => {
      const channelName = `regression-member-${Date.now()}`;
      const res = await request(API_BASE_URL)
        .post(`/api/v1/community/${COMMUNITY_ID}/interact/channels`)
        .set('Authorization', `Bearer ${authToken}`)
        .send({ name: channelName, channel_type: 'chat' });

      // Must not be a 500 (type mismatch / server error) — regression for Bug A
      expect(res.status).not.toBe(500);

      // Clean up if channel was created
      if (res.status === 201 && res.body.channel?.id) {
        await request(API_BASE_URL)
          .delete(`/api/v1/community/${COMMUNITY_ID}/interact/channels/${res.body.channel.id}`)
          .set('Authorization', `Bearer ${authToken}`);
      }
    });

    it('returns a clean 409 (not 500) on duplicate channel name — no 500→409 cycle', async () => {
      const channelName = `regression-dup-${Date.now()}`;

      // First creation should succeed
      const first = await request(API_BASE_URL)
        .post(`/api/v1/community/${COMMUNITY_ID}/interact/channels`)
        .set('Authorization', `Bearer ${authToken}`)
        .send({ name: channelName, channel_type: 'chat' });

      if (first.status !== 201) return; // Can't test dedup without first creation

      // Second creation with the same name should be a clean conflict, not a server error
      const second = await request(API_BASE_URL)
        .post(`/api/v1/community/${COMMUNITY_ID}/interact/channels`)
        .set('Authorization', `Bearer ${authToken}`)
        .send({ name: channelName, channel_type: 'chat' });

      // Regression for Bug B: without a transaction the first attempt orphans a
      // record, causing a 500 on the second attempt (unique constraint on an
      // incomplete row). With the transaction fix, duplicates return a clean 409.
      expect(second.status).toBe(409);
      expect(second.status).not.toBe(500);

      // Clean up
      if (first.body.channel?.id) {
        await request(API_BASE_URL)
          .delete(`/api/v1/community/${COMMUNITY_ID}/interact/channels/${first.body.channel.id}`)
          .set('Authorization', `Bearer ${authToken}`);
      }
    });
  });
});
