import { HomeIcon } from '@heroicons/react/24/outline';
import { Card, EmptyState, PageHeader } from '../../components/ui';

function PlatformCommunities() {
  return (
    <div>
      <PageHeader title="Platform Communities" />
      <Card>
        <EmptyState
          icon={<HomeIcon />}
          title="Community management is not built yet"
          description="Platform-wide community administration will live here. Super admins can manage communities today from Super Admin > Communities."
        />
      </Card>
    </div>
  );
}

export default PlatformCommunities;
