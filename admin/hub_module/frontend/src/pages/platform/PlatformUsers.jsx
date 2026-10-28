import { UserGroupIcon } from '@heroicons/react/24/outline';
import { Card, EmptyState, PageHeader } from '../../components/ui';

function PlatformUsers() {
  return (
    <div>
      <PageHeader title="Platform Users" />
      <Card>
        <EmptyState
          icon={<UserGroupIcon />}
          title="User management is not built yet"
          description="Platform-wide user administration will live here. Super admins can manage users today from Super Admin > User Management."
        />
      </Card>
    </div>
  );
}

export default PlatformUsers;
