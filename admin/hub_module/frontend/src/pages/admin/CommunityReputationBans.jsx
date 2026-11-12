import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { Alert, Badge, Button, Card, CardBody, EmptyState, LoadingState, PageHeader, TextField } from '../../components/ui';
import { serverErrorMessage } from '../../lib/reputationPolicy';

const fmt = (iso) => (iso ? new Date(iso).toLocaleString() : '-');

/**
 * Soft-banned members and at-risk members for a community, with manual soft-ban and clear
 * actions. Thin view over the admin reputation soft-bans / at-risk APIs.
 */
function CommunityReputationBans() {
  const { communityId } = useParams();
  const [bans, setBans] = useState(null);
  const [atRisk, setAtRisk] = useState(null);
  const [error, setError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [userId, setUserId] = useState('');
  const [expires, setExpires] = useState('');

  const load = useCallback(async () => {
    setError(null);
    setBans(null);
    setAtRisk(null);
    try {
      const [b, a] = await Promise.all([
        adminApi.getReputationSoftBans(communityId),
        adminApi.getAtRiskUsers(communityId),
      ]);
      setBans(b.data.softBans || []);
      setAtRisk(a.data.members || []);
    } catch (err) {
      setError({ status: err?.response?.status, message: serverErrorMessage(err, 'Failed to load reputation data') });
    }
  }, [communityId]);

  useEffect(() => {
    load();
  }, [load]);

  async function run(fn, okMessage) {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(okMessage);
      await load();
    } catch (err) {
      setActionError(serverErrorMessage(err, 'Action failed'));
    }
    setBusy(false);
  }

  const softBan = (id, expiresAt) =>
    run(() => adminApi.setReputationSoftBan(communityId, id, expiresAt), 'Member soft-banned.');
  const clearBan = (id) => run(() => adminApi.clearReputationSoftBan(communityId, id), 'Soft ban cleared.');

  function handleManual(e) {
    e.preventDefault();
    if (!/^\d+$/.test(userId.trim())) {
      setActionError('Member ID must be a numeric hub user ID');
      return;
    }
    softBan(userId.trim(), expires ? new Date(expires).toISOString() : undefined);
  }

  if (error) {
    return (
      <div className="p-6" data-testid="rep-load-error">
        <Alert tone="danger">
          {error.status === 403
            ? `You do not have permission to manage reputation for this community. ${error.message}`
            : error.message}
        </Alert>
        {error.status !== 403 && <Button className="mt-3" onClick={load}>Retry</Button>}
      </div>
    );
  }
  if (!bans || !atRisk) return <div className="p-6"><LoadingState label="Loading reputation data" /></div>;

  return (
    <div className="p-6 space-y-8">
      <PageHeader
        title="Soft bans and at-risk members"
        description="Soft-banned members cannot win games, raffles or giveaways but remain in the community."
      />
      {actionError && <div data-testid="rep-action-error"><Alert tone="danger">{actionError}</Alert></div>}
      {notice && <Alert tone="success">{notice}</Alert>}

      <Card>
        <CardBody>
          <h2 className="text-lg font-semibold mb-3">Soft-banned members</h2>
          {bans.length === 0 ? (
            <EmptyState title="No soft-banned members" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm" data-testid="soft-ban-table">
                <thead>
                  <tr className="text-left">
                    <th>Member</th><th>Reason</th><th>Set by</th><th>Since</th><th>Expires</th><th><span className="sr-only">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  {bans.map((b) => (
                    <tr key={b.userId}>
                      <td>{b.displayName}</td>
                      <td><Badge>{b.reason === 'manual' ? 'Manual' : 'Score floor'}</Badge></td>
                      <td>{b.setByType}</td>
                      <td>{fmt(b.setAt)}</td>
                      <td>{b.expiresAt ? fmt(b.expiresAt) : 'Until cleared'}</td>
                      <td>
                        <Button size="sm" variant="outline" disabled={busy} onClick={() => clearBan(b.userId)}>
                          Clear soft ban
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <form onSubmit={handleManual} className="mt-6 flex flex-wrap items-end gap-3" noValidate>
            <TextField label="Member ID" value={userId} onChange={(e) => setUserId(e.target.value)} hint="Hub user ID" />
            <TextField label="Expires (optional)" type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} />
            <Button type="submit" variant="danger" disabled={busy}>Soft-ban member</Button>
          </form>
        </CardBody>
      </Card>

      <Card>
        <CardBody>
          <h2 className="text-lg font-semibold mb-3">At-risk members</h2>
          {atRisk.length === 0 ? (
            <EmptyState title="No at-risk members" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm" data-testid="at-risk-table">
                <thead>
                  <tr className="text-left"><th>Member</th><th>Score</th><th>Tier</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr>
                </thead>
                <tbody>
                  {atRisk.map((m) => (
                    <tr key={m.userId}>
                      <td>{m.displayName}</td>
                      <td>{m.score}</td>
                      <td>{m.tier}</td>
                      <td>
                        {m.belowHardBanFloor ? 'Below hard-ban floor' : m.belowSoftBanFloor ? 'Below soft-ban floor' : 'At risk'}
                      </td>
                      <td>
                        <Button size="sm" variant="outline" disabled={busy || !m.userId} onClick={() => softBan(m.userId)}>
                          Soft-ban
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

export default CommunityReputationBans;
