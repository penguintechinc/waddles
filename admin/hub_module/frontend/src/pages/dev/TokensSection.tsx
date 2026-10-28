import { Card, CardBody, CardHeader } from '../../components/ui';

interface Swatch {
  name: string;
  cls: string;
  note: string;
}

const SURFACES: Swatch[] = [
  { name: 'app', cls: 'bg-app', note: 'Page canvas' },
  { name: 'surface', cls: 'bg-surface', note: 'Cards, sidebar' },
  { name: 'surface-raised', cls: 'bg-surface-raised', note: 'Hover, popovers, table head' },
  { name: 'surface-field', cls: 'bg-surface-field', note: 'Input wells' },
];

const STATUS: Swatch[] = [
  { name: 'accent', cls: 'bg-accent', note: 'Primary action (gold)' },
  { name: 'interactive', cls: 'bg-interactive', note: 'Links, focus (sky)' },
  { name: 'success', cls: 'bg-success', note: 'Positive' },
  { name: 'warning', cls: 'bg-warning', note: 'Caution' },
  { name: 'danger', cls: 'bg-danger', note: 'Destructive' },
  { name: 'info', cls: 'bg-info', note: 'Neutral info' },
];

const TEXT = [
  { cls: 'text-fg', label: 'text-fg', sample: 'Primary text for headings and body' },
  { cls: 'text-fg-secondary', label: 'text-fg-secondary', sample: 'Secondary text for descriptions' },
  { cls: 'text-fg-muted', label: 'text-fg-muted', sample: 'Muted text for hints and metadata' },
  { cls: 'text-fg-accent', label: 'text-fg-accent', sample: 'Accent text for emphasis and active nav' },
];

/** Renders one labelled swatch row. */
function SwatchGrid({ swatches }: { swatches: Swatch[] }) {
  return (
    <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      {swatches.map((s) => (
        <li key={s.name}>
          <div className={`h-14 rounded-control border border-line ${s.cls}`} aria-hidden="true" />
          <p className="mt-1.5 text-xs font-medium text-fg">{s.name}</p>
          <p className="text-2xs text-fg-muted">{s.note}</p>
        </li>
      ))}
    </ul>
  );
}

/** Color, type and elevation tokens as a visual reference. */
export function TokensSection() {
  return (
    <Card as="section" aria-labelledby="ds-tokens">
      <CardHeader title="Design tokens" description="Semantic names over the navy / sky / gold / emerald brand scales." />
      <CardBody className="space-y-8">
        <div>
          <h3 id="ds-tokens" className="eyebrow mb-3">Surfaces</h3>
          <SwatchGrid swatches={SURFACES} />
        </div>
        <div>
          <h3 className="eyebrow mb-3">Intent</h3>
          <SwatchGrid swatches={STATUS} />
        </div>
        <div>
          <h3 className="eyebrow mb-3">Text</h3>
          <ul className="space-y-1.5">
            {TEXT.map((t) => (
              <li key={t.cls} className="flex flex-wrap items-baseline gap-x-4">
                <code className="w-44 shrink-0 font-mono text-xs text-fg-muted">{t.label}</code>
                <span className={`text-sm ${t.cls}`}>{t.sample}</span>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h3 className="eyebrow mb-3">Type scale</h3>
          <p className="page-title">Page title - 2xl semibold</p>
          <p className="section-title mt-2">Section title - base semibold</p>
          <p className="mt-2 text-sm text-fg">Body - sm regular. Dense admin tables and forms use this size.</p>
          <p className="mt-2 text-xs text-fg-secondary">Caption - xs, for metadata and helper text.</p>
          <p className="eyebrow mt-2">Eyebrow - xs uppercase tracking</p>
        </div>
        <div>
          <h3 className="eyebrow mb-3">Elevation</h3>
          <div className="flex flex-wrap gap-4">
            {(['shadow-card', 'shadow-raised', 'shadow-overlay'] as const).map((s) => (
              <div key={s} className={`flex h-16 w-32 items-center justify-center rounded-card bg-surface-raised text-xs text-fg-secondary ${s}`}>
                {s}
              </div>
            ))}
          </div>
        </div>
      </CardBody>
    </Card>
  );
}
