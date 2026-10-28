/**
 * Proves the accessibility analyzer can FAIL: each defect class is detected on a
 * fixture, and the compliant form of the same markup is not. Without this, a
 * zero in the budget test could just mean the analyzer is blind.
 */
import { describe, expect, it } from 'vitest';
import { analyzeSource } from '../lib/analyze.mjs';

const run = (jsx) => analyzeSource(`export default function X(){ return (${jsx}); }`, 'x.jsx');

describe('analyzeSource', () => {
  it('flags unlabeled controls and accepts every labelling technique', () => {
    expect(run('<input type="text" />').unlabeled).toHaveLength(1);
    expect(run('<select><option>a</option></select>').unlabeled).toHaveLength(1);
    expect(run('<textarea />').unlabeled).toHaveLength(1);
    expect(run('<input aria-label="Name" />').unlabeled).toHaveLength(0);
    expect(run('<input aria-labelledby="x" />').unlabeled).toHaveLength(0);
    expect(run('<label>Name <input /></label>').unlabeled).toHaveLength(0);
    expect(run('<div><label htmlFor="n">Name</label><input id="n" /></div>').unlabeled).toHaveLength(0);
    expect(run('<div><label htmlFor="n">Name</label><input id="other" /></div>').unlabeled).toHaveLength(1);
    expect(run('<input type="hidden" />').unlabeled).toHaveLength(0);
    expect(run('<input type="submit" />').unlabeled).toHaveLength(0);
  });

  it('flags images without alt but accepts empty alt', () => {
    expect(run('<img src="a.png" />').imgNoAlt).toHaveLength(1);
    expect(run('<img src="a.png" alt="" />').imgNoAlt).toHaveLength(0);
    expect(run('<img src="a.png" alt="Logo" />').imgNoAlt).toHaveLength(0);
  });

  it('flags icon-only buttons (including conditional icons) without a name', () => {
    expect(run('<button><TrashIcon /></button>').iconButtons).toHaveLength(1);
    expect(run('<button><svg /></button>').iconButtons).toHaveLength(1);
    expect(run('<button>{open ? <XIcon /> : <MenuIcon />}</button>').iconButtons).toHaveLength(1);
    expect(run('<button aria-label="Delete"><TrashIcon /></button>').iconButtons).toHaveLength(0);
    expect(run('<button title="Delete"><TrashIcon /></button>').iconButtons).toHaveLength(0);
    expect(run('<button><TrashIcon /> Delete</button>').iconButtons).toHaveLength(0);
  });

  it('flags clickable non-interactive elements unless keyboard-operable or delegated', () => {
    expect(run('<div onClick={go}>x</div>').clickable).toHaveLength(1);
    expect(run('<tr onClick={go}><td>x</td></tr>').clickable).toHaveLength(1);
    expect(run('<div role="button" tabIndex={0} onClick={go}>x</div>').clickable).toHaveLength(0);
    expect(run('<div role="presentation" onClick={close}>x</div>').clickable).toHaveLength(0);
    expect(run('<tr data-click-delegate onClick={go}><td><button>x</button></td></tr>').clickable).toHaveLength(0);
    expect(run('<button onClick={go}>x</button>').clickable).toHaveLength(0);
  });

  it('flags focus-outline removal that has no replacement ring', () => {
    const bare = run('<input className="focus:outline-none" />').outline;
    expect(bare).toHaveLength(1);
    expect(bare[0].replaced).toBe(false);
    expect(run('<input className="focus:outline-none focus:ring-2" />').outline[0].replaced).toBe(true);
    expect(run('<input className="outline-none focus-visible:ring-2" />').outline[0].replaced).toBe(true);
    expect(run('<main tabIndex={-1} className="focus:outline-none" />').outline[0].replaced).toBe(true);
    expect(run('<input className={`a ${x ? "focus:outline-none" : ""}`} />').outline[0].replaced).toBe(false);
  });

  it('flags modal overlays without dialog semantics', () => {
    expect(run('<div className="fixed inset-0"><div><h2>Title</h2></div></div>').overlays).toHaveLength(1);
    expect(run('<div className="fixed inset-0"><div role="dialog" aria-modal="true"><h2>T</h2></div></div>').overlays).toHaveLength(0);
    expect(run('<div className="fixed inset-0" role="dialog" />').overlays).toHaveLength(0);
  });

  it('counts motion utilities', () => {
    expect(run('<div className="animate-spin" />').animate).toBe(1);
    expect(run('<div className="p-4" />').animate).toBe(0);
  });
});
