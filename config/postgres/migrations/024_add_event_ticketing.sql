-- Migration: 024_add_event_ticketing.sql
-- Description: Add ticketing, check-in, and payment configuration for calendar events
-- Author: Claude Code
-- Date: 2025-12-18

-- ============================================================================
-- PHASE 1: Extend calendar_events table for ticketing
-- ============================================================================

-- Fresh-DB replay: no migration in either chain ever CREATEd calendar_events
-- (historically created out-of-band by the calendar module / PyDAL), so on an empty
-- database every ALTER below -- and 061_calendar_sync -- hit UndefinedTable. Create it
-- first, with the same column set as alembic 0039_event_sync_enabled.py and
-- hub_api bind_calendar_sync_tables(); IF NOT EXISTS keeps this a no-op on any DB
-- where the table already exists (all already-migrated DBs skip this file entirely).
CREATE TABLE IF NOT EXISTS calendar_events (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    event_date TIMESTAMPTZ NOT NULL,
    end_date TIMESTAMPTZ,
    timezone VARCHAR(100),
    location TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'scheduled',
    discord_event_id VARCHAR(255),
    sync_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    sync_error TEXT,
    last_sync_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- calendar_rsvps is referenced by calendar_tickets.rsvp_id below and likewise has no
-- CREATE TABLE anywhere; columns are the ones calendar_interaction_module's
-- rsvp_service.py reads/writes.
CREATE TABLE IF NOT EXISTS calendar_rsvps (
    id SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,
    platform_user_id VARCHAR(100) NOT NULL,
    rsvp_status VARCHAR(20) NOT NULL DEFAULT 'yes',
    guest_count INTEGER NOT NULL DEFAULT 0,
    user_note TEXT,
    is_waitlisted BOOLEAN NOT NULL DEFAULT FALSE,
    waitlist_position INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (event_id, platform, platform_user_id)
);

ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS ticketing_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS require_ticket BOOLEAN DEFAULT FALSE;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS is_paid_event BOOLEAN DEFAULT FALSE;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS ticket_sales_start TIMESTAMPTZ;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS ticket_sales_end TIMESTAMPTZ;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS event_type VARCHAR(20) DEFAULT 'virtual';
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS check_in_mode VARCHAR(20) DEFAULT 'admin_only';
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS refund_policy JSONB DEFAULT '{}';

-- Add constraint for event_type
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'valid_event_type'
    ) THEN
        ALTER TABLE calendar_events ADD CONSTRAINT valid_event_type
            CHECK (event_type IN ('virtual', 'in_person', 'hybrid'));
    END IF;
END$$;

-- Add constraint for check_in_mode
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'valid_check_in_mode'
    ) THEN
        ALTER TABLE calendar_events ADD CONSTRAINT valid_check_in_mode
            CHECK (check_in_mode IN ('admin_only', 'self_checkin', 'auto_checkin'));
    END IF;
END$$;

COMMENT ON COLUMN calendar_events.ticketing_enabled IS 'Whether ticketing is enabled for this event';
COMMENT ON COLUMN calendar_events.require_ticket IS 'Whether a ticket is required to attend (vs just RSVP)';
COMMENT ON COLUMN calendar_events.is_paid_event IS 'Whether tickets are paid (requires premium)';
COMMENT ON COLUMN calendar_events.event_type IS 'Event type: virtual, in_person, or hybrid';
COMMENT ON COLUMN calendar_events.check_in_mode IS 'How attendees check in: admin_only, self_checkin, or auto_checkin';
COMMENT ON COLUMN calendar_events.refund_policy IS 'Event-level refund policy overrides (JSON)';

-- ============================================================================
-- PHASE 2: Create ticket types table
-- ============================================================================

CREATE TABLE IF NOT EXISTS calendar_ticket_types (
    id SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,

    -- Ticket type details
    name VARCHAR(100) NOT NULL,
    description TEXT,

    -- Capacity management
    max_quantity INTEGER,  -- NULL = unlimited
    sold_count INTEGER DEFAULT 0,
    reserved_count INTEGER DEFAULT 0,

    -- Pricing (for premium communities)
    is_paid BOOLEAN DEFAULT FALSE,
    price_cents INTEGER DEFAULT 0,
    currency VARCHAR(3) DEFAULT 'USD',

    -- Availability window
    sales_start TIMESTAMPTZ,
    sales_end TIMESTAMPTZ,

    -- Display settings
    display_order INTEGER DEFAULT 0,
    is_visible BOOLEAN DEFAULT TRUE,
    is_active BOOLEAN DEFAULT TRUE,

    -- Metadata
    metadata JSONB DEFAULT '{}',

    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- Constraints
    UNIQUE(event_id, name),
    CONSTRAINT valid_price CHECK (price_cents >= 0),
    CONSTRAINT valid_quantity CHECK (max_quantity IS NULL OR max_quantity > 0),
    CONSTRAINT valid_sold_count CHECK (sold_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_ticket_types_event ON calendar_ticket_types(event_id, is_active);
CREATE INDEX IF NOT EXISTS idx_ticket_types_sales ON calendar_ticket_types(event_id, sales_start, sales_end)
    WHERE is_active = TRUE AND is_visible = TRUE;

-- Trigger to update updated_at
CREATE OR REPLACE TRIGGER update_calendar_ticket_types_updated_at
    BEFORE UPDATE ON calendar_ticket_types
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE calendar_ticket_types IS 'Ticket type definitions for events (General, VIP, Early Bird, etc.)';

-- ============================================================================
-- PHASE 3: Create tickets table
-- ============================================================================

CREATE TABLE IF NOT EXISTS calendar_tickets (
    id SERIAL PRIMARY KEY,
    ticket_uuid UUID UNIQUE NOT NULL DEFAULT gen_random_uuid(),
    ticket_code VARCHAR(64) UNIQUE NOT NULL,  -- 64-char hex for QR verification
    ticket_number INTEGER NOT NULL,  -- Sequential per event (#001, #002, etc.)

    -- Event and type references
    event_id INTEGER NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
    ticket_type_id INTEGER REFERENCES calendar_ticket_types(id) ON DELETE SET NULL,
    rsvp_id INTEGER REFERENCES calendar_rsvps(id) ON DELETE SET NULL,

    -- Ticket holder information
    hub_user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    holder_name VARCHAR(255),
    holder_email VARCHAR(255),
    platform VARCHAR(50) NOT NULL,
    platform_user_id VARCHAR(100) NOT NULL,
    username VARCHAR(100) NOT NULL,

    -- Ticket status
    status VARCHAR(20) DEFAULT 'valid',

    -- Check-in tracking
    is_checked_in BOOLEAN DEFAULT FALSE,
    checked_in_at TIMESTAMPTZ,
    checked_in_by INTEGER REFERENCES hub_users(id),
    check_in_method VARCHAR(20),
    check_in_location VARCHAR(255),
    check_in_notes TEXT,

    -- Payment tracking (for premium communities)
    is_paid BOOLEAN DEFAULT FALSE,
    payment_provider VARCHAR(20),
    payment_id VARCHAR(255),
    payment_amount_cents INTEGER DEFAULT 0,
    payment_currency VARCHAR(3) DEFAULT 'USD',
    payment_status VARCHAR(20),
    paid_at TIMESTAMPTZ,
    refunded_at TIMESTAMPTZ,
    refund_amount_cents INTEGER DEFAULT 0,

    -- Guest tracking (for +1 tickets)
    guest_number INTEGER DEFAULT 1,
    primary_ticket_id INTEGER REFERENCES calendar_tickets(id) ON DELETE CASCADE,

    -- Transfer tracking (admin-only transfers)
    transferred_from_ticket_id INTEGER REFERENCES calendar_tickets(id),
    transferred_at TIMESTAMPTZ,
    transferred_by INTEGER REFERENCES hub_users(id),
    transfer_notes TEXT,

    -- Cancellation tracking
    cancelled_at TIMESTAMPTZ,
    cancelled_by INTEGER REFERENCES hub_users(id),
    cancelled_reason TEXT,

    -- Metadata
    metadata JSONB DEFAULT '{}',

    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- Constraints
    UNIQUE(event_id, ticket_number),
    CONSTRAINT valid_ticket_status CHECK (status IN ('valid', 'checked_in', 'cancelled', 'expired', 'refunded', 'transferred')),
    CONSTRAINT valid_payment_status CHECK (payment_status IS NULL OR payment_status IN ('pending', 'processing', 'completed', 'failed', 'refunded', 'partially_refunded')),
    CONSTRAINT valid_check_in_method CHECK (check_in_method IS NULL OR check_in_method IN ('qr_scan', 'manual', 'api', 'self_checkin', 'auto_checkin')),
    CONSTRAINT valid_guest_number CHECK (guest_number >= 1)
);

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_tickets_event ON calendar_tickets(event_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_code ON calendar_tickets(ticket_code);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON calendar_tickets(hub_user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_rsvp ON calendar_tickets(rsvp_id);
CREATE INDEX IF NOT EXISTS idx_tickets_checkin ON calendar_tickets(event_id, is_checked_in);
CREATE INDEX IF NOT EXISTS idx_tickets_payment ON calendar_tickets(payment_provider, payment_id) WHERE payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tickets_status ON calendar_tickets(event_id, status, created_at DESC);

-- Trigger to update updated_at
CREATE OR REPLACE TRIGGER update_calendar_tickets_updated_at
    BEFORE UPDATE ON calendar_tickets
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE calendar_tickets IS 'Individual tickets with unique 64-char hex codes for QR verification';
COMMENT ON COLUMN calendar_tickets.ticket_code IS '64-char hex code for QR verification (256 bits entropy)';
COMMENT ON COLUMN calendar_tickets.ticket_number IS 'Sequential ticket number per event for display (#001, #002)';
COMMENT ON COLUMN calendar_tickets.check_in_method IS 'How this ticket was checked in: qr_scan, manual, api, self_checkin, auto_checkin';

-- ============================================================================
-- PHASE 4: Create check-in audit log table
-- ============================================================================

CREATE TABLE IF NOT EXISTS calendar_ticket_check_ins (
    id SERIAL PRIMARY KEY,
    ticket_id INTEGER REFERENCES calendar_tickets(id) ON DELETE SET NULL,
    event_id INTEGER NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,

    -- Action details
    action VARCHAR(20) NOT NULL,
    ticket_code VARCHAR(64),

    -- Result
    success BOOLEAN NOT NULL,
    result_code VARCHAR(50),
    failure_reason TEXT,

    -- Operator info (who performed the check-in)
    operator_user_id INTEGER REFERENCES hub_users(id),
    operator_username VARCHAR(100),
    operator_role VARCHAR(50),

    -- Ticket holder snapshot (for audit trail)
    holder_username VARCHAR(100),
    holder_hub_user_id INTEGER,
    ticket_type_name VARCHAR(100),

    -- Context
    scan_method VARCHAR(20) NOT NULL,
    device_info JSONB DEFAULT '{}',
    ip_address INET,
    user_agent TEXT,
    location VARCHAR(255),

    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_check_ins_event ON calendar_ticket_check_ins(event_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_check_ins_ticket ON calendar_ticket_check_ins(ticket_id);
CREATE INDEX IF NOT EXISTS idx_check_ins_operator ON calendar_ticket_check_ins(operator_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_check_ins_result ON calendar_ticket_check_ins(event_id, success, created_at DESC);

-- Add constraint for action types
ALTER TABLE calendar_ticket_check_ins ADD CONSTRAINT valid_checkin_action
    CHECK (action IN ('check_in', 'undo_check_in', 'self_check_in', 'auto_check_in', 'rejected'));
ALTER TABLE calendar_ticket_check_ins ADD CONSTRAINT valid_scan_method
    CHECK (scan_method IN ('qr_scan', 'manual_entry', 'api', 'self_checkin', 'auto_checkin'));

COMMENT ON TABLE calendar_ticket_check_ins IS 'Audit log for all ticket check-in attempts (AAA compliance)';
COMMENT ON COLUMN calendar_ticket_check_ins.result_code IS 'Machine-readable result: success, already_checked_in, invalid_ticket, wrong_event, expired, cancelled';

-- ============================================================================
-- PHASE 5: Create community payment configuration table
-- ============================================================================

CREATE TABLE IF NOT EXISTS calendar_payment_config (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL UNIQUE REFERENCES communities(id) ON DELETE CASCADE,

    -- Provider selection
    payment_provider VARCHAR(20),

    -- Stripe configuration (encrypted in production)
    stripe_enabled BOOLEAN DEFAULT FALSE,
    stripe_account_id VARCHAR(255),
    stripe_publishable_key VARCHAR(255),
    stripe_webhook_secret VARCHAR(255),
    stripe_onboarding_complete BOOLEAN DEFAULT FALSE,

    -- PayPal configuration (encrypted in production)
    paypal_enabled BOOLEAN DEFAULT FALSE,
    paypal_client_id VARCHAR(255),
    paypal_merchant_id VARCHAR(255),
    paypal_onboarding_complete BOOLEAN DEFAULT FALSE,

    -- General settings
    default_currency VARCHAR(3) DEFAULT 'USD',
    platform_fee_percent DECIMAL(5,2) DEFAULT 2.0,

    -- Refund policy defaults
    default_refund_policy JSONB DEFAULT '{
        "allow_refunds": true,
        "refund_before_hours": 24,
        "refund_percent": 100,
        "no_refund_after_start": true
    }',

    -- Status
    is_verified BOOLEAN DEFAULT FALSE,
    verified_at TIMESTAMPTZ,
    verification_notes TEXT,

    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Trigger to update updated_at
CREATE OR REPLACE TRIGGER update_calendar_payment_config_updated_at
    BEFORE UPDATE ON calendar_payment_config
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE calendar_payment_config IS 'Per-community payment provider configuration for paid tickets (premium feature)';
COMMENT ON COLUMN calendar_payment_config.stripe_account_id IS 'Stripe Connect account ID for receiving payments';
COMMENT ON COLUMN calendar_payment_config.platform_fee_percent IS 'Platform fee percentage (default 2%)';

-- ============================================================================
-- PHASE 6: Add default_refund_policy to communities table
-- ============================================================================

ALTER TABLE communities ADD COLUMN IF NOT EXISTS default_refund_policy JSONB DEFAULT '{
    "allow_refunds": true,
    "refund_before_hours": 24,
    "refund_percent": 100,
    "no_refund_after_start": true
}';

COMMENT ON COLUMN communities.default_refund_policy IS 'Community-level default refund policy for paid events';

-- ============================================================================
-- PHASE 7: Helper function to generate unique ticket numbers
-- ============================================================================

CREATE OR REPLACE FUNCTION get_next_ticket_number(p_event_id INTEGER)
RETURNS INTEGER AS $$
DECLARE
    next_num INTEGER;
BEGIN
    SELECT COALESCE(MAX(ticket_number), 0) + 1
    INTO next_num
    FROM calendar_tickets
    WHERE event_id = p_event_id;

    RETURN next_num;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_next_ticket_number IS 'Get next sequential ticket number for an event';

-- ============================================================================
-- PHASE 8: Insert default ticket type for events with ticketing
-- ============================================================================

-- Function to create default ticket type when ticketing is enabled
CREATE OR REPLACE FUNCTION create_default_ticket_type()
RETURNS TRIGGER AS $$
BEGIN
    -- Only create default if ticketing was just enabled and no types exist
    IF NEW.ticketing_enabled = TRUE AND (OLD.ticketing_enabled IS NULL OR OLD.ticketing_enabled = FALSE) THEN
        INSERT INTO calendar_ticket_types (event_id, name, description, display_order)
        VALUES (NEW.id, 'General Admission', 'Standard event ticket', 1)
        ON CONFLICT (event_id, name) DO NOTHING;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER create_default_ticket_type_trigger
    AFTER INSERT OR UPDATE OF ticketing_enabled ON calendar_events
    FOR EACH ROW
    WHEN (NEW.ticketing_enabled = TRUE)
    EXECUTE FUNCTION create_default_ticket_type();

-- ============================================================================
-- PHASE 9: Create event admins table (per-event scoped roles)
-- ============================================================================

CREATE TABLE IF NOT EXISTS calendar_event_admins (
    id SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,

    -- User identification
    hub_user_id INTEGER REFERENCES hub_users(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,
    platform_user_id VARCHAR(100) NOT NULL,
    username VARCHAR(100) NOT NULL,

    -- Permissions granted (can be customized per assignment)
    can_edit_event BOOLEAN DEFAULT TRUE,
    can_check_in BOOLEAN DEFAULT TRUE,
    can_view_tickets BOOLEAN DEFAULT TRUE,
    can_manage_ticket_types BOOLEAN DEFAULT FALSE,
    can_cancel_tickets BOOLEAN DEFAULT FALSE,
    can_transfer_tickets BOOLEAN DEFAULT FALSE,
    can_export_attendance BOOLEAN DEFAULT TRUE,
    can_assign_event_admins BOOLEAN DEFAULT FALSE,

    -- Assignment metadata
    assigned_by INTEGER REFERENCES hub_users(id),
    assigned_by_username VARCHAR(100),
    assignment_notes TEXT,

    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    revoked_at TIMESTAMPTZ,
    revoked_by INTEGER REFERENCES hub_users(id),
    revoked_reason TEXT,

    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- Ensure one assignment per user per event
    UNIQUE(event_id, platform, platform_user_id)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_event_admins_event ON calendar_event_admins(event_id, is_active);
CREATE INDEX IF NOT EXISTS idx_event_admins_user ON calendar_event_admins(hub_user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_event_admins_platform ON calendar_event_admins(platform, platform_user_id, is_active);

-- Trigger to update updated_at
CREATE OR REPLACE TRIGGER update_calendar_event_admins_updated_at
    BEFORE UPDATE ON calendar_event_admins
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE calendar_event_admins IS 'Per-event admin role assignments with granular permissions';
COMMENT ON COLUMN calendar_event_admins.can_edit_event IS 'Can edit event details (title, description, etc.)';
COMMENT ON COLUMN calendar_event_admins.can_check_in IS 'Can check in attendees via QR scan or manual entry';
COMMENT ON COLUMN calendar_event_admins.can_view_tickets IS 'Can view ticket list and attendee details';
COMMENT ON COLUMN calendar_event_admins.can_manage_ticket_types IS 'Can create/edit/delete ticket types';
COMMENT ON COLUMN calendar_event_admins.can_cancel_tickets IS 'Can cancel tickets';
COMMENT ON COLUMN calendar_event_admins.can_transfer_tickets IS 'Can transfer tickets between users';
COMMENT ON COLUMN calendar_event_admins.can_export_attendance IS 'Can export attendance reports';
COMMENT ON COLUMN calendar_event_admins.can_assign_event_admins IS 'Can assign other event admins (requires community admin for initial assignment)';

-- ============================================================================
-- Migration complete
-- ============================================================================

-- Log migration
DO $$
BEGIN
    RAISE NOTICE 'Migration 024_add_event_ticketing completed successfully';
END$$;
