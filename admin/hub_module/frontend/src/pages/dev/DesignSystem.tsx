import { PageHeader } from '../../components/ui';
import { ControlsSection } from './ControlsSection';
import { DataSection } from './DataSection';
import { TokensSection } from './TokensSection';

/**
 * Living style guide for the Waddles Hub design system (development builds
 * only, mounted at /design-system). Everything here renders the real shared
 * primitives, so it doubles as a visual regression target at 320/768/1024px.
 */
export default function DesignSystem() {
  return (
    <div className="mx-auto max-w-6xl space-y-8 px-4 py-10 sm:px-6 lg:px-8">
      <PageHeader title="Design system" description="Tokens, primitives and states used across the Waddles Hub." />
      <TokensSection />
      <ControlsSection />
      <DataSection />
    </div>
  );
}
