/**
 * Tests for the site-wide announcement banner: markdown-link rendering,
 * enabled/empty gating, silent failure, per-text dismissal persistence, and
 * the module-level fetch cache. Each case loads a fresh module graph so the
 * cache starts empty.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

let activeRtl;

async function load(getBanner) {
  vi.resetModules();
  vi.doMock('../../services/api', () => ({ publicApi: { getBanner } }));
  const rtl = await import('@testing-library/react');
  activeRtl = rtl;
  const { default: GlobalBanner } = await import('../GlobalBanner');
  return { ...rtl, GlobalBanner };
}

const BANNER = {
  enabled: true,
  text: 'Maintenance at 5pm, see [status page](https://status.example.com/x) or [docs](http://docs.example.com) now',
  bgColor: '#112233',
  textColor: '#ffeedd',
};

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  // Auto-cleanup only hooks the first import of RTL; each case re-imports it.
  activeRtl?.cleanup();
  vi.doUnmock('../../services/api');
  localStorage.clear();
});

describe('GlobalBanner', () => {
  it('renders banner text with markdown links turned into safe anchors', async () => {
    const getBanner = vi.fn().mockResolvedValue({ data: BANNER });
    const { render, screen, GlobalBanner } = await load(getBanner);

    const { container } = render(<GlobalBanner />);

    const status = await screen.findByRole('link', { name: 'status page' });
    expect(status).toHaveAttribute('href', 'https://status.example.com/x');
    expect(status).toHaveAttribute('target', '_blank');
    expect(status).toHaveAttribute('rel', 'noopener noreferrer');
    expect(status).toHaveStyle({ color: '#ffeedd' });
    expect(screen.getByRole('link', { name: 'docs' })).toHaveAttribute('href', 'http://docs.example.com');
    expect(container.firstChild).toHaveStyle({ backgroundColor: '#112233', color: '#ffeedd' });
    expect(container.firstChild).toHaveTextContent('Maintenance at 5pm, see status page or docs now');
  });

  it('does not turn non-http markdown links into anchors', async () => {
    const getBanner = vi.fn().mockResolvedValue({
      data: { ...BANNER, text: 'click [here](javascript:alert(1)) please' },
    });
    const { render, screen, GlobalBanner } = await load(getBanner);

    render(<GlobalBanner />);

    expect(await screen.findByText(/click \[here\]\(javascript:alert\(1\)\) please/)).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it.each([
    ['disabled', { ...BANNER, enabled: false }],
    ['textless', { ...BANNER, text: '' }],
  ])('renders nothing for a %s banner', async (_label, data) => {
    const getBanner = vi.fn().mockResolvedValue({ data });
    const { render, waitFor, GlobalBanner } = await load(getBanner);

    const { container } = render(<GlobalBanner />);

    await waitFor(() => expect(getBanner).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing and does not throw when the banner request fails', async () => {
    const getBanner = vi.fn().mockRejectedValue(new Error('offline'));
    const { render, waitFor, GlobalBanner } = await load(getBanner);

    const { container } = render(<GlobalBanner />);

    await waitFor(() => expect(getBanner).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('dismisses on click and remembers the dismissal for that exact text', async () => {
    const getBanner = vi.fn().mockResolvedValue({ data: BANNER });
    const { render, screen, fireEvent, GlobalBanner } = await load(getBanner);

    const first = render(<GlobalBanner />);
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss banner' }));

    expect(first.container).toBeEmptyDOMElement();
    const keys = Object.keys(localStorage).filter((k) => k.startsWith('banner_dismissed_'));
    expect(keys).toHaveLength(1);
    expect(localStorage.getItem(keys[0])).toBe('1');
    first.unmount();

    const second = render(<GlobalBanner />);
    expect(second.container).toBeEmptyDOMElement();
    expect(getBanner).toHaveBeenCalledTimes(1);
  });

  it('shows a banner whose text differs from a previously dismissed one', async () => {
    const getBanner = vi.fn().mockResolvedValue({ data: { ...BANNER, text: 'Brand new notice' } });
    localStorage.setItem('banner_dismissed_somethingelse', '1');
    const { render, screen, GlobalBanner } = await load(getBanner);

    render(<GlobalBanner />);

    expect(await screen.findByText('Brand new notice')).toBeInTheDocument();
  });

  it('fetches once and reuses the cached banner across remounts', async () => {
    const getBanner = vi.fn().mockResolvedValue({ data: BANNER });
    const { render, screen, waitFor, GlobalBanner } = await load(getBanner);

    const first = render(<GlobalBanner />);
    await screen.findByRole('link', { name: 'docs' });
    first.unmount();

    render(<GlobalBanner />);
    await waitFor(() => expect(screen.getByRole('link', { name: 'docs' })).toBeInTheDocument());
    expect(getBanner).toHaveBeenCalledTimes(1);
  });
});
