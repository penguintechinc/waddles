-- Migration 053: Register all platform commands (global defaults)
--
-- Seeds the commands table with global default entries for all modules across
-- Discord (slash), Slack (slash), and Twitch (! prefix) platforms.
-- community_id = NULL means these are global defaults; communities can override
-- individually. Uses ON CONFLICT DO NOTHING to be idempotent / safe to re-run.
--
-- Permission levels: everyone, registered, moderator, admin, owner

INSERT INTO commands
    (command, module_name, description, usage, category, permission_level, cooldown_seconds)
VALUES

-- ── Forms ─────────────────────────────────────────────────────────────────
('!form',       'forms', 'List or submit a community form',         '!form [name]',              'community',   'registered',  5),
('/form',       'forms', 'List or submit a community form',         '/form [name]',              'community',   'registered',  5),

-- ── Polls ─────────────────────────────────────────────────────────────────
('!poll',       'polls', 'View or vote on community polls',         '!poll [list|vote <name>]',  'community',   'registered',  3),
('/poll',       'polls', 'View or vote on community polls',         '/poll [list|vote <name>]',  'community',   'registered',  3),

-- ── Support / Tickets ─────────────────────────────────────────────────────
('!ticket',     'support', 'Create or check a support ticket',      '!ticket <description>',     'support',     'registered',  30),
('/ticket',     'support', 'Create or check a support ticket',      '/ticket [new|status]',      'support',     'registered',  30),

-- ── Loyalty / Currency ────────────────────────────────────────────────────
('!balance',    'loyalty', 'Check your loyalty point balance',      '!balance',                  'loyalty',     'everyone',    5),
('/balance',    'loyalty', 'Check your loyalty point balance',      '/balance',                  'loyalty',     'everyone',    5),
('!give',       'loyalty', 'Give loyalty points to another user',   '!give @user <amount>',      'loyalty',     'registered',  10),
('/give',       'loyalty', 'Give loyalty points to another user',   '/give @user <amount>',      'loyalty',     'registered',  10),
('!slots',      'loyalty', 'Play the slots minigame',               '!slots [bet]',              'loyalty',     'registered',  30),
('/slots',      'loyalty', 'Play the slots minigame',               '/slots [bet]',              'loyalty',     'registered',  30),
('!duel',       'loyalty', 'Challenge a user to a loyalty duel',    '!duel @user <bet>',         'loyalty',     'registered',  60),
('/duel',       'loyalty', 'Challenge a user to a loyalty duel',    '/duel @user <bet>',         'loyalty',     'registered',  60),
('!giveaway',   'loyalty', 'Start or enter a loyalty giveaway',     '!giveaway [start|enter]',   'loyalty',     'registered',  0),
('/giveaway',   'loyalty', 'Start or enter a loyalty giveaway',     '/giveaway [start|enter]',   'loyalty',     'registered',  0),

-- ── Memories / Quotes / Bookmarks / Reminders ────────────────────────────
('!quote',      'memories', 'Add or retrieve community quotes',     '!quote [add <text>|random|search <term>]', 'community', 'everyone',   5),
('/quote',      'memories', 'Add or retrieve community quotes',     '/quote [add|random|search]', 'community',  'everyone',   5),
('!bookmark',   'memories', 'Save a URL bookmark',                  '!bookmark <url> [tags]',    'community',   'registered',  10),
('/bookmark',   'memories', 'Save a URL bookmark',                  '/bookmark <url> [tags]',    'community',   'registered',  10),
('!remind',     'memories', 'Set a personal reminder',              '!remind <time> <message>',  'utility',     'registered',  5),
('/remind',     'memories', 'Set a personal reminder',              '/remind <time> <message>',  'utility',     'registered',  5),

-- ── Looking for Group ─────────────────────────────────────────────────────
('!lfg',        'lfg', 'Looking for group — create or join a party', '!lfg [create|list|join <id>|leave|cancel]', 'gaming', 'registered', 10),
('/lfg',        'lfg', 'Looking for group — create or join a party', '/lfg [create|list|join|leave|cancel]',      'gaming', 'registered', 10),

-- ── Calendar / Events ─────────────────────────────────────────────────────
('!event',      'calendar', 'View upcoming community events',       '!event [list|view <id>]',   'calendar',    'everyone',   5),
('/event',      'calendar', 'View upcoming community events',       '/event [list|view]',        'calendar',    'everyone',   5),
('!rsvp',       'calendar', 'RSVP to a community event',           '!rsvp <event_id>',          'calendar',    'registered',  5),
('/rsvp',       'calendar', 'RSVP to a community event',           '/rsvp <event_id>',          'calendar',    'registered',  5),

-- ── Shoutout ──────────────────────────────────────────────────────────────
('!so',         'shoutout', 'Give a shoutout to a user',           '!so <username>',            'streaming',   'moderator',  15),
('/so',         'shoutout', 'Give a shoutout to a user',           '/so @user',                 'streaming',   'moderator',  15),

-- ── Translation ───────────────────────────────────────────────────────────
('!translate',  'translate', 'Translate text to a target language', '!translate <lang> <text>',  'utility',     'everyone',   5),
('/translate',  'translate', 'Translate text to a target language', '/translate <lang> <text>',  'utility',     'everyone',   5),

-- ── Server / Game Status ─────────────────────────────────────────────────
('!status',     'server_status', 'Check game server status',       '!status [game]',            'gaming',      'everyone',   10),
('/status',     'server_status', 'Check game server status',       '/status [game]',            'gaming',      'everyone',   10),

-- ── Clips ─────────────────────────────────────────────────────────────────
('!clip',       'clip', 'Create or bookmark a stream clip',        '!clip [list|highlight <id>]', 'streaming', 'everyone',   15),
('/clip',       'clip', 'Create or bookmark a stream clip',        '/clip [list|bookmark|highlight]', 'streaming', 'everyone', 15),

-- ── Alias ─────────────────────────────────────────────────────────────────
('!alias',      'alias', 'Manage custom command aliases',          '!alias [create <name> <cmd>|list|delete <name>]', 'utility', 'registered', 5),
('/alias',      'alias', 'Manage custom command aliases',          '/alias [create|list|delete]', 'utility',   'registered',  5),

-- ── AI / Ask ──────────────────────────────────────────────────────────────
('!ask',        'ai_insights', 'Ask the AI a question',            '!ask <question>',           'ai',          'everyone',   10),
('/ask',        'ai_insights', 'Ask the AI a question',            '/ask <question>',           'ai',          'everyone',   10),

-- ── Reputation ────────────────────────────────────────────────────────────
('!rep',        'reputation', 'View a user''s reputation score',   '!rep [@user]',              'moderation',  'everyone',   5),
('/rep',        'reputation', 'View a user''s reputation score',   '/rep [@user]',              'moderation',  'everyone',   5),

-- ── Labels ────────────────────────────────────────────────────────────────
('!label',      'labels', 'Add or list user labels (mod only)',    '!label add @user <label>',  'moderation',  'moderator',  5),
('/label',      'labels', 'Add or list user labels (mod only)',    '/label add @user <label>',  'moderation',  'moderator',  5),

-- ── Leaderboard ───────────────────────────────────────────────────────────
('!top',        'leaderboard', 'View community leaderboard',       '!top [category]',           'community',   'everyone',   10),
('/top',        'leaderboard', 'View community leaderboard',       '/top [category]',           'community',   'everyone',   10),

-- ── Context Switching ─────────────────────────────────────────────────────
('!context',    'router', 'View or switch your community context', '!context [<community>|reset|default <community>]', 'utility', 'everyone', 3),
('/context',    'router', 'View or switch your community context', '/context [switch|reset|default]', 'utility', 'everyone', 3),

-- ── Linking / Server Management ───────────────────────────────────────────
('!join',       'router', 'Request to link this channel to a community', '!join <community_name>', 'admin',  'owner',       0),
('/join',       'router', 'Request to link this server to a community',  '/join <community_name>', 'admin',  'owner',       0),
('!approve',    'router', 'Approve a pending community link request', '!approve <community_name>', 'admin',  'owner',       0),
('/approve',    'router', 'Approve a pending community link request', '/approve <community_name>', 'admin',  'owner',       0),
('!leave',      'router', 'Remove link between this channel and a community', '!leave <community>', 'admin', 'owner',      0),
('/leave',      'router', 'Remove link between this server and a community',  '/leave <community>', 'admin', 'owner',      0),
('!linked',     'router', 'List communities linked to this channel', '!linked',                 'admin',     'everyone',   5),
('/linked',     'router', 'List communities linked to this server',  '/linked',                 'admin',     'everyone',   5),
('!link',       'router', 'Manage link settings for this channel',  '!link [status|default <community>]', 'admin', 'everyone', 5),
('/link',       'router', 'Manage link settings for this server',   '/link [status|default <community>]', 'admin', 'everyone', 5)

ON CONFLICT (command, community_id) DO NOTHING;

ANALYZE commands;
