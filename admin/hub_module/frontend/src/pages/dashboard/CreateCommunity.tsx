import { useMutation } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import CommunityForm from '../../components/CommunityForm';
import { getErrorMessage } from '../../components/ui/errors';
import { useAuthView } from '../../contexts/authView';
import { createCommunity, toCreatePayload } from '../../services/communityAdminApi';

/** Create-community page: super admins use the platform route, everyone else the self-service route. */
export default function CreateCommunity() {
  const navigate = useNavigate();
  const { isSuperAdmin } = useAuthView();
  const create = useMutation({
    mutationFn: (raw: unknown) => createCommunity(toCreatePayload(raw, isSuperAdmin), isSuperAdmin),
    onSuccess: (community) => navigate(`/dashboard/community/${community.id}`),
  });

  return (
    <CommunityForm
      onSubmit={async (formData: unknown) => {
        await create.mutateAsync(formData).catch(() => undefined);
      }}
      saving={create.isPending}
      error={create.isError ? getErrorMessage(create.error, 'Failed to create community') : null}
      showOwnerFields={isSuperAdmin}
      backLink={isSuperAdmin ? '/superadmin/communities' : '/dashboard'}
    />
  );
}
