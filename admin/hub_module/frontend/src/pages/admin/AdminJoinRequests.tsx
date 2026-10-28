import { useParams } from 'react-router-dom';
import { CheckIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { Alert, Button, DataTable, ErrorState, PageHeader, type Column } from '../../components/ui';
import { getErrorMessage } from '../../components/ui/errors';
import { usePendingJoinRequests, useReviewJoinRequest } from '../../hooks/useJoinRequests';
import type { JoinRequestRow } from '../../services/joinRequestsApi';

/** `Oct 21, 2026`; empty when the timestamp does not parse. */
function formatRequested(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString();
}

/** Pending join-request queue with approve/reject, on the served `/admin/:id/join-requests` routes. */
function JoinRequestsQueue({ communityId }: { communityId: number }) {
  const query = usePendingJoinRequests(communityId);
  const review = useReviewJoinRequest(communityId);
  const busy = review.isPending ? review.variables : undefined;

  const columns: readonly Column<JoinRequestRow>[] = [
    {
      key: 'user',
      header: 'User',
      cell: (row) => (
        <>
          <div className="font-medium text-fg">{row.username}</div>
          {row.email ? <div className="text-xs text-fg-muted">{row.email}</div> : null}
        </>
      ),
    },
    {
      key: 'message',
      header: 'Message',
      className: 'max-w-xs text-sm text-fg-secondary',
      cell: (row) => row.message ?? <span className="italic text-fg-muted">No message</span>,
    },
    { key: 'requested', header: 'Requested', className: 'text-sm text-fg-secondary', cell: (row) => formatRequested(row.createdAt) },
    {
      key: 'actions',
      header: 'Actions',
      cell: (row) => {
        const working = busy?.requestId === row.id;
        return (
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="success"
              loading={working && busy?.decision === 'approve'}
              disabled={review.isPending}
              leadingIcon={<CheckIcon className="h-3.5 w-3.5" />}
              onClick={() => review.mutate({ requestId: row.id, decision: 'approve' })}
              aria-label={`Approve ${row.username}`}
            >
              Approve
            </Button>
            <Button
              size="sm"
              variant="danger"
              loading={working && busy?.decision === 'reject'}
              disabled={review.isPending}
              leadingIcon={<XMarkIcon className="h-3.5 w-3.5" />}
              onClick={() => review.mutate({ requestId: row.id, decision: 'reject' })}
              aria-label={`Reject ${row.username}`}
            >
              Reject
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <div>
      <PageHeader
        title="Join Requests"
        actions={
          <Button onClick={() => void query.refetch()} loading={query.isFetching && !query.isPending}>
            Refresh
          </Button>
        }
      />
      {review.isError ? (
        <Alert tone="danger" title="We could not update that request" className="mb-4" onDismiss={() => review.reset()}>
          {getErrorMessage(review.error, 'The request was not updated.')}
        </Alert>
      ) : null}
      <DataTable
        caption="Pending join requests"
        columns={columns}
        rows={query.data?.requests}
        getRowId={(row) => row.id}
        loading={query.isPending}
        error={query.isError ? query.error : undefined}
        onRetry={() => void query.refetch()}
        emptyTitle="No pending join requests"
      />
    </div>
  );
}

/** Route wrapper: validates `:communityId` before any query runs. */
export default function AdminJoinRequests() {
  const { communityId } = useParams<{ communityId: string }>();
  const id = Number(communityId);
  if (!Number.isInteger(id) || id <= 0) {
    return <ErrorState title="Unknown community" message="This join requests link is not valid." />;
  }
  return <JoinRequestsQueue communityId={id} />;
}
