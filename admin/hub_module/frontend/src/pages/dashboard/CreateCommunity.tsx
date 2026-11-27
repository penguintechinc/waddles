import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import CommunityForm from '../../components/CommunityForm';
import { NotAvailableYet } from '../../components/ui';
import { getErrorMessage } from '../../components/ui/errors';
import { useAuthView } from '../../contexts/authView';
import { communityKeys } from '../../hooks/useMyCommunities';
import { createCommunity, toCreatePayload } from '../../services/communityAdminApi';

/** Create-community page: super admins create on the served route; everyone else sees why not. */
export default function CreateCommunity() {
  const navigate = useNavigate();
  const { isSuperAdmin } = useAuthView();
  const queryClient = useQueryClient();
  const create = useMutation({
    mutationFn: (raw: unknown) => createCommunity(toCreatePayload(raw, isSuperAdmin)),
    onSuccess: async (community) => {
      // The creator is now a member (admin role): refetch "my communities" instead of serving the
      // 5-minute-stale list that does not contain the new community yet.
      await queryClient.invalidateQueries({ queryKey: communityKeys.mine });
      navigate(`/dashboard/community/${community.id}`);
    },
  });

  if (!isSuperAdmin) {
    return (
      <NotAvailableYet
        feature="community-self-create"
        description="Communities can only be created by platform administrators for now. Ask an admin to set one up for you."
      />
    );
  }

  return (
    <CommunityForm
      onSubmit={async (formData: unknown) => {
        await create.mutateAsync(formData).catch(() => undefined);
      }}
      saving={create.isPending}
      error={create.isError ? getErrorMessage(create.error, 'Failed to create community') : null}
      showOwnerFields
      backLink="/superadmin/communities"
    />
  );
}
