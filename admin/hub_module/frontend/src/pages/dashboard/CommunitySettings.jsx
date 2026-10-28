import { Navigate, useParams } from 'react-router-dom';

/**
 * Community settings live in the admin panel (profile, roles, domains, ...).
 * This route is linked from the member dashboard's "Community Admin" group, so it
 * forwards there instead of rendering a stub.
 */
function CommunitySettings() {
  const { id } = useParams();
  return <Navigate to={`/admin/${id}/profile`} replace />;
}

export default CommunitySettings;
