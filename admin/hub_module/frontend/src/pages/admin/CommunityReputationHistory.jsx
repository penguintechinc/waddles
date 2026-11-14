import { useCallback, useEffect, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { Alert, Button, Card, CardBody, EmptyState, LoadingState, PageHeader } from '../../components/ui';
import { serverErrorMessage } from '../../lib/reputationPolicy';

/**
 * Per-member reputation ledger (newest first) for a community, selected via `?userId=`.
 * Thin view over GET /api/v1/admin/:id/reputation/history.
 */
function CommunityReputationHistory() {
  const { communityId } = useParams();
  const [params] = useSearchParams();
  const userId = params.get('userId');
  const [entries, setEntries] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    setEntries(null);
    try {
      const res = await adminApi.getReputationHistory(communityId, userId ? { userId } : undefined);
      setEntries(res.data.entries || []);
    } catch (err) {
      setError({ status: err?.response?.status, message: serverErrorMessage(err, 'Failed to load history') });
    }
  }, [communityId, userId]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="p-6">
      <PageHeader
        title="Reputation history"
        description={userId ? `Ledger for member ${userId}, newest first.` : 'Community ledger, newest first.'}
        actions={<Link to={`/admin/${communityId}/reputation/bans`} className="btn btn-secondary">Back</Link>}
      />
      {error && (
        <div data-testid="rep-load-error">
          <Alert tone="danger">
            {error.status === 403 ? `You do not have permission to view reputation history. ${error.message}` : error.message}
          </Alert>
          {error.status !== 403 && <Button className="mt-3" onClick={load}>Retry</Button>}
        </div>
      )}
      {!error && !entries && <LoadingState label="Loading reputation history" />}
      {entries && (
        <Card>
          <CardBody>
            {entries.length === 0 ? (
              <EmptyState title="No reputation history" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm" data-testid="history-table">
                  <thead>
                    <tr className="text-left"><th>When</th><th>Change</th><th>Balance</th><th>Kind</th><th>Actor</th><th>Source</th></tr>
                  </thead>
                  <tbody>
                    {entries.map((e) => (
                      <tr key={e.id}>
                        <td>{e.occurredAt ? new Date(e.occurredAt).toLocaleString() : '-'}</td>
                        <td>{e.delta > 0 ? `+${e.delta}` : e.delta}</td>
                        <td>{e.balanceAfter ?? '-'}</td>
                        <td>{e.kind}</td>
                        <td>{e.actorType}</td>
                        <td>{e.sourceEvent || '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardBody>
        </Card>
      )}
    </div>
  );
}

export default CommunityReputationHistory;
