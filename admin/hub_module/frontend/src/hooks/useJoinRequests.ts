import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from '@tanstack/react-query';
import {
  fetchJoinRequests,
  fetchMyJoinRequest,
  reviewJoinRequest,
  submitJoinRequest,
  type JoinRequestPage,
  type MyJoinRequest,
} from '../services/joinRequestsApi';
import { communityKeys } from './useMyCommunities';

/** Query keys for join-request reads. */
export const joinRequestKeys = {
  mine: (communityId: number) => ['community', communityId, 'join-request', 'mine'] as const,
  pending: (communityId: number) => ['community', communityId, 'join-requests', 'pending'] as const,
};

/** The caller's own request for a community; `enabled` is false for signed-out visitors. */
export function useMyJoinRequest(communityId: number, enabled: boolean): UseQueryResult<MyJoinRequest | null> {
  return useQuery({
    queryKey: joinRequestKeys.mine(communityId),
    queryFn: () => fetchMyJoinRequest(communityId),
    enabled,
  });
}

/** Submit (or reopen) a join request, then refresh the caller's request status. */
export function useSubmitJoinRequest(communityId: number): UseMutationResult<MyJoinRequest, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (message: string) => submitJoinRequest(communityId, message),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: joinRequestKeys.mine(communityId) }),
  });
}

/** Pending join requests for the admin queue. */
export function usePendingJoinRequests(communityId: number): UseQueryResult<JoinRequestPage> {
  return useQuery({ queryKey: joinRequestKeys.pending(communityId), queryFn: () => fetchJoinRequests(communityId) });
}

export interface ReviewJoinRequestVariables {
  requestId: number;
  decision: 'approve' | 'reject';
}

/** Approve/reject; refreshes the queue, and (on approve) the member's own community list. */
export function useReviewJoinRequest(communityId: number): UseMutationResult<void, Error, ReviewJoinRequestVariables> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ requestId, decision }: ReviewJoinRequestVariables) => reviewJoinRequest(communityId, requestId, decision),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: joinRequestKeys.pending(communityId) }),
        queryClient.invalidateQueries({ queryKey: communityKeys.mine }),
      ]),
  });
}
