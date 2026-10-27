//! Direct SeaORM CRUD tests for the hand-written entities in
//! `src/db/entities/` against an in-memory sqlite DB -- independent of the
//! HTTP layer (`tests/api_*.rs` cover that), proving the entities
//! themselves insert/select/update/delete correctly against DDL matching
//! migration 079 (`streaming_configs`/`streaming_targets`) plus the
//! minimal `tenants`/`communities` slice `crate::api::tenancy` reads.

use sea_orm::{
    ActiveModelTrait, ColumnTrait, Database, DatabaseConnection, EntityTrait, QueryFilter, Set,
};

use svc_streaming::db::entities::{community, streaming_config, streaming_target, tenant};

async fn seed_db() -> DatabaseConnection {
    let db = Database::connect("sqlite::memory:")
        .await
        .expect("connect in-memory sqlite");
    let schema = r#"
        CREATE TABLE tenants (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE);
        CREATE TABLE communities (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL);
        CREATE TABLE streaming_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id INTEGER NOT NULL UNIQUE,
            source_url TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'rtmp',
            enabled INTEGER NOT NULL DEFAULT 1,
            record_enabled INTEGER NOT NULL DEFAULT 0,
            transcode_enabled INTEGER NOT NULL DEFAULT 0,
            transcode_bitrate_kbps INTEGER NOT NULL DEFAULT 4000,
            video_codec TEXT NOT NULL DEFAULT 'h264',
            audio_codec TEXT NOT NULL DEFAULT 'copy'
        );
        CREATE TABLE streaming_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            forward_url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            protocol TEXT NOT NULL DEFAULT 'rtmp',
            video_codec TEXT,
            audio_codec TEXT
        );
        INSERT INTO tenants (id, slug) VALUES (1, 'tenant-abc');
        INSERT INTO communities (id, tenant_id) VALUES (10, 1);
    "#;
    for stmt in schema.split(';') {
        let stmt = stmt.trim();
        if !stmt.is_empty() {
            sea_orm::ConnectionTrait::execute_unprepared(&db, stmt)
                .await
                .unwrap_or_else(|err| panic!("seed statement failed ({stmt:?}): {err}"));
        }
    }
    db
}

#[tokio::test]
async fn tenant_and_community_resolve_by_slug_and_id() {
    let db = seed_db().await;

    let t = tenant::Entity::find()
        .filter(tenant::Column::Slug.eq("tenant-abc"))
        .one(&db)
        .await
        .unwrap()
        .expect("seeded tenant");
    assert_eq!(t.id, 1);

    let c = community::Entity::find_by_id(10)
        .one(&db)
        .await
        .unwrap()
        .expect("seeded community");
    assert_eq!(c.tenant_id, 1);
}

#[tokio::test]
async fn streaming_config_insert_select_update_delete_round_trips() {
    let db = seed_db().await;

    let active = streaming_config::ActiveModel {
        community_id: Set(10),
        source_url: Set("rtmp://ingest.example/live".into()),
        source_type: Set("rtmp".into()),
        enabled: Set(true),
        record_enabled: Set(false),
        transcode_enabled: Set(true),
        transcode_bitrate_kbps: Set(5000),
        ..Default::default()
    };
    let inserted = active.insert(&db).await.expect("insert config");
    assert!(inserted.id > 0);
    assert_eq!(inserted.transcode_bitrate_kbps, 5000);

    let fetched = streaming_config::Entity::find_by_id(inserted.id)
        .filter(streaming_config::Column::CommunityId.eq(10))
        .one(&db)
        .await
        .unwrap()
        .expect("row exists");
    assert_eq!(fetched.source_url, "rtmp://ingest.example/live");
    assert!(fetched.transcode_enabled);

    let mut update: streaming_config::ActiveModel = fetched.into();
    update.enabled = Set(false);
    let updated = update.update(&db).await.expect("update config");
    assert!(!updated.enabled);

    let active: streaming_config::ActiveModel = updated.into();
    active.delete(&db).await.expect("delete config");
    let gone = streaming_config::Entity::find_by_id(inserted.id)
        .one(&db)
        .await
        .unwrap();
    assert!(gone.is_none());
}

#[tokio::test]
async fn streaming_target_forward_url_column_holds_serialized_secret_ref_only() {
    let db = seed_db().await;

    let config = streaming_config::ActiveModel {
        community_id: Set(10),
        source_url: Set("rtmp://a".into()),
        source_type: Set("rtmp".into()),
        enabled: Set(true),
        record_enabled: Set(false),
        transcode_enabled: Set(false),
        transcode_bitrate_kbps: Set(4000),
        ..Default::default()
    }
    .insert(&db)
    .await
    .unwrap();

    let secret_ref_json = serde_json::to_string(&svc_streaming::store::SecretRef::Env {
        var: "RELAY_URL".into(),
    })
    .unwrap();
    let target = streaming_target::ActiveModel {
        config_id: Set(config.id),
        platform: Set("custom".into()),
        forward_url: Set(secret_ref_json.clone()),
        enabled: Set(true),
        ..Default::default()
    }
    .insert(&db)
    .await
    .expect("insert target");

    assert_eq!(target.forward_url, secret_ref_json);
    // Never a raw scheme -- only the serialized secret_ref tag/value.
    assert!(!target.forward_url.starts_with("rtmp://"));

    let listed = streaming_target::Entity::find()
        .filter(streaming_target::Column::ConfigId.eq(config.id))
        .all(&db)
        .await
        .unwrap();
    assert_eq!(listed.len(), 1);
    let parsed: svc_streaming::store::SecretRef =
        serde_json::from_str(&listed[0].forward_url).unwrap();
    assert!(matches!(
        parsed,
        svc_streaming::store::SecretRef::Env { var } if var == "RELAY_URL"
    ));
}
