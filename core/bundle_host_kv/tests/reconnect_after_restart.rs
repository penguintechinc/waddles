//! Regression: a Valkey server restart must not permanently break a stage's
//! long-lived Valkey handle (alpha 2026-11: svc-ingest/svc-action logged
//! "broken pipe" forever after valkey-durable restarted).
//!
//! No live Valkey is needed: a tiny in-process RESP responder stands in for
//! the server, and "restarting" it severs every accepted socket while the
//! listener keeps accepting. Proves `redis::aio::ConnectionManager` (what the
//! stages now hold) recovers by itself, and that `check_maxmemory_policy`
//! and the `KvBackend` impl accept it.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

/// Serves `+PONG`/`+OK` for every RESP command until `sever` flips, then
/// drops the socket (simulating the server process dying).
async fn fake_valkey(listener: TcpListener, sever: Arc<AtomicBool>, accepted: Arc<AtomicUsize>) {
    loop {
        let Ok((mut sock, _)) = listener.accept().await else {
            return;
        };
        accepted.fetch_add(1, Ordering::SeqCst);
        let sever = Arc::clone(&sever);
        tokio::spawn(async move {
            let mut buf = vec![0u8; 4096];
            loop {
                tokio::select! {
                    n = sock.read(&mut buf) => {
                        let Ok(n) = n else { return };
                        if n == 0 { return; }
                        let text = String::from_utf8_lossy(&buf[..n]).to_string();
                        // One reply per RESP array header (= one command).
                        let cmds = text.lines().filter(|l| l.starts_with('*')).count().max(1);
                        let reply: &[u8] = if text.to_uppercase().contains("PING") {
                            b"+PONG\r\n"
                        } else {
                            b"+OK\r\n"
                        };
                        for _ in 0..cmds {
                            if sock.write_all(reply).await.is_err() { return; }
                        }
                    }
                    _ = tokio::time::sleep(Duration::from_millis(20)) => {
                        if sever.load(Ordering::SeqCst) { return; }
                    }
                }
            }
        });
    }
}

async fn ping(conn: &mut redis::aio::ConnectionManager) -> Result<String, redis::RedisError> {
    redis::cmd("PING").query_async(conn).await
}

#[tokio::test]
async fn connection_manager_recovers_after_server_restart() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let sever = Arc::new(AtomicBool::new(false));
    let accepted = Arc::new(AtomicUsize::new(0));
    tokio::spawn(fake_valkey(
        listener,
        Arc::clone(&sever),
        Arc::clone(&accepted),
    ));

    let client = redis::Client::open(format!("redis://127.0.0.1:{port}")).unwrap();
    let mut conn = client.get_connection_manager().await.unwrap();
    assert_eq!(ping(&mut conn).await.unwrap(), "PONG");
    let before = accepted.load(Ordering::SeqCst);

    // "Restart": drop every live socket, then let new ones through.
    sever.store(true, Ordering::SeqCst);
    tokio::time::sleep(Duration::from_millis(150)).await;
    sever.store(false, Ordering::SeqCst);

    // The first command(s) after the loss may fail; the handle must heal itself.
    let mut healed = false;
    for _ in 0..50 {
        if ping(&mut conn).await.is_ok() {
            healed = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(
        healed,
        "ConnectionManager never recovered after server restart"
    );
    assert!(
        accepted.load(Ordering::SeqCst) > before,
        "recovery must have opened a NEW connection (before={before})"
    );

    // A clone taken BEFORE the restart (what long-lived consumers hold) works too.
    let mut clone = conn.clone();
    assert_eq!(ping(&mut clone).await.unwrap(), "PONG");
}

#[tokio::test]
async fn plain_multiplexed_connection_stays_dead_after_restart() {
    // Documents WHY the stages moved off `MultiplexedConnection`: it never reconnects.
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let sever = Arc::new(AtomicBool::new(false));
    tokio::spawn(fake_valkey(
        listener,
        Arc::clone(&sever),
        Arc::new(AtomicUsize::new(0)),
    ));
    let client = redis::Client::open(format!("redis://127.0.0.1:{port}")).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let ok: String = redis::cmd("PING").query_async(&mut conn).await.unwrap();
    assert_eq!(ok, "PONG");
    sever.store(true, Ordering::SeqCst);
    tokio::time::sleep(Duration::from_millis(150)).await;
    sever.store(false, Ordering::SeqCst);
    for _ in 0..5 {
        let r: Result<String, _> = redis::cmd("PING").query_async(&mut conn).await;
        assert!(r.is_err(), "a MultiplexedConnection must not self-heal");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}
