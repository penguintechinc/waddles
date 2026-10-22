/**
 * AST-based accessibility analyzer for hub-webui JSX/TSX sources.
 *
 * One analyzer feeds both the audit ratchet (`scripts/ui-audit.mjs`) and any
 * codemods, so "how many are left" and "what do we fix" can never disagree.
 * Findings carry source offsets so callers can splice fixes without
 * reformatting the file.
 */
import { parse } from '@babel/parser';
import traverseModule from '@babel/traverse';

const traverse = traverseModule.default ?? traverseModule;

const CONTROL_TAGS = new Set(['input', 'select', 'textarea']);
const NON_LABELLED_INPUT_TYPES = new Set(['hidden', 'submit', 'button', 'reset', 'image']);
const CLICKABLE_NON_INTERACTIVE = new Set(['div', 'span', 'li', 'tr', 'td', 'p', 'section', 'article', 'img', 'h1', 'h2', 'h3', 'h4']);

/** Parses JSX/TSX with error recovery so one odd file never aborts a survey. */
export function parseSource(src, file) {
  const plugins = ['jsx'];
  if (/\.tsx?$/.test(file)) plugins.push('typescript');
  return parse(src, { sourceType: 'module', plugins, errorRecovery: true });
}

/** `<a.b>` / `<a>` name as a string. */
export function tagName(nameNode) {
  if (nameNode.type === 'JSXIdentifier') return nameNode.name;
  if (nameNode.type === 'JSXMemberExpression') return `${tagName(nameNode.object)}.${nameNode.property.name}`;
  return '';
}

/** Finds a (non-spread) attribute on an opening element. */
export function getAttr(opening, name) {
  return opening.attributes.find((a) => a.type === 'JSXAttribute' && a.name.name === name);
}

/** True when the opening element spreads props (`{...props}`), so attributes are unknowable. */
export function hasSpread(opening) {
  return opening.attributes.some((a) => a.type === 'JSXSpreadAttribute');
}

/** Static string value of an attribute (`"x"`, `{'x'}`, `` {`x`} `` without expressions), else null. */
export function attrString(attr) {
  if (!attr || !attr.value) return null;
  const v = attr.value;
  if (v.type === 'StringLiteral') return v.value;
  if (v.type === 'JSXExpressionContainer') {
    const e = v.expression;
    if (e.type === 'StringLiteral') return e.value;
    if (e.type === 'TemplateLiteral' && e.expressions.length === 0) return e.quasis.map((q) => q.value.cooked).join('');
  }
  return null;
}

/** All literal text fragments of a className attribute (string, template quasis, nested string literals). */
export function classFragments(attr) {
  if (!attr || !attr.value) return [];
  const out = [];
  const visit = (n) => {
    if (!n || typeof n !== 'object') return;
    if (n.type === 'StringLiteral') out.push({ text: n.value, start: n.start, end: n.end, kind: 'string' });
    else if (n.type === 'TemplateElement') out.push({ text: n.value.raw, start: n.start, end: n.end, kind: 'template' });
    else for (const k of Object.keys(n)) if (k !== 'loc' && k !== 'extra') visit(n[k]);
  };
  visit(attr.value);
  return out;
}

/** Plain-text label content of a `<label>` element, or null when dynamic/empty. */
export function labelText(labelElement) {
  let text = '';
  for (const child of labelElement.children) {
    if (child.type === 'JSXText') text += child.value;
    else if (child.type === 'JSXExpressionContainer') {
      const e = child.expression;
      if (e.type === 'StringLiteral') text += e.value;
      else if (e.type === 'TemplateLiteral' && e.expressions.length === 0) text += e.quasis.map((q) => q.value.cooked).join('');
      else if (e.type !== 'JSXEmptyExpression') return null;
    } else if (child.type === 'JSXElement') {
      // Decorative children such as <span>*</span> required markers.
      const inner = labelText(child);
      if (inner === null) return null;
      text += inner;
    }
  }
  const cleaned = text.replace(/\s+/g, ' ').replace(/[*:]+\s*$/g, '').trim();
  return cleaned === '' ? null : cleaned;
}

/** Collects every static `htmlFor` target in the file. */
function collectLabelTargets(ast) {
  const targets = new Set();
  traverse(ast, {
    JSXOpeningElement(path) {
      if (tagName(path.node.name) !== 'label') return;
      const attr = getAttr(path.node, 'htmlFor');
      if (!attr) return;
      const lit = attrString(attr);
      if (lit) targets.add(lit);
      else if (attr.value?.type === 'JSXExpressionContainer') targets.add(`{expr}${attr.value.expression.start}`);
    },
  });
  return targets;
}

/** Whether `path` is a descendant of a `<label>` element. */
function insideLabel(path) {
  return Boolean(path.findParent((p) => p.isJSXElement() && tagName(p.node.openingElement.name) === 'label'));
}

/** Whether a JSX element is an icon (`<svg>` or a `*Icon` component). */
function isIconElement(node) {
  if (!node || node.type !== 'JSXElement') return false;
  const name = tagName(node.openingElement.name);
  return name === 'svg' || /Icon$/.test(name);
}

/** True for an expression that can only ever render an icon (icon, `c ? icon : icon`, `c && icon`). */
function isIconExpression(expr) {
  if (!expr) return false;
  if (expr.type === 'JSXElement') return isIconElement(expr);
  if (expr.type === 'ConditionalExpression') return isIconExpression(expr.consequent) && isIconExpression(expr.alternate);
  if (expr.type === 'LogicalExpression') return isIconExpression(expr.right);
  if (expr.type === 'ParenthesizedExpression') return isIconExpression(expr.expression);
  return false;
}

/** True for a button whose only content is icon components / svg (no text). */
function isIconOnlyButton(element) {
  let sawIcon = false;
  for (const child of element.children) {
    if (child.type === 'JSXText') {
      if (child.value.trim() !== '') return false;
    } else if (child.type === 'JSXElement') {
      if (isIconElement(child)) sawIcon = true;
      else return false;
    } else if (child.type === 'JSXExpressionContainer') {
      if (child.expression.type === 'JSXEmptyExpression') continue;
      if (isIconExpression(child.expression)) sawIcon = true;
      else return false;
    } else return false;
  }
  return sawIcon;
}

/**
 * Analyze one source file.
 * @returns findings arrays; each entry has `line`, `start`, `end` plus detail.
 */
export function analyzeSource(src, file) {
  const ast = parseSource(src, file);
  const targets = collectLabelTargets(ast);
  const findings = { unlabeled: [], imgNoAlt: [], iconButtons: [], clickable: [], outline: [], animate: 0 };

  traverse(ast, {
    JSXElement(path) {
      const el = path.node;
      const opening = el.openingElement;
      const name = tagName(opening.name);
      const line = opening.loc?.start.line ?? 0;

      if (CONTROL_TAGS.has(name)) {
        const type = attrString(getAttr(opening, 'type'));
        if (name === 'input' && type && NON_LABELLED_INPUT_TYPES.has(type)) return;
        if (hasSpread(opening)) return;
        if (getAttr(opening, 'aria-label') || getAttr(opening, 'aria-labelledby')) return;
        const idAttr = getAttr(opening, 'id');
        if (idAttr) {
          const lit = attrString(idAttr);
          if (lit && targets.has(lit)) return;
          if (!lit && idAttr.value?.type === 'JSXExpressionContainer') {
            // Dynamic id: assume a matching dynamic htmlFor exists when the file has any.
            if ([...targets].some((t) => t.startsWith('{expr}'))) return;
          }
        }
        if (insideLabel(path)) return;
        findings.unlabeled.push({ line, start: opening.start, end: opening.end, tag: name, type, path, element: el });
        return;
      }

      if (name === 'img' && !getAttr(opening, 'alt') && !hasSpread(opening)) {
        findings.imgNoAlt.push({ line, start: opening.start, end: opening.end });
      }

      if (name === 'button' && !hasSpread(opening)) {
        const named = getAttr(opening, 'aria-label') || getAttr(opening, 'aria-labelledby') || getAttr(opening, 'title');
        if (!named && isIconOnlyButton(el)) {
          findings.iconButtons.push({ line, start: opening.start, end: opening.end, element: el });
        }
      }

      if (CLICKABLE_NON_INTERACTIVE.has(name) && getAttr(opening, 'onClick')) {
        // Exemptions: (1) role+tabIndex (keyboard-operable), (2) role=presentation|none
        // (modal backdrops), (3) `data-click-delegate`: the click is a mouse shortcut for a
        // real <button>/<a> inside the element, which is the keyboard path.
        const role = attrString(getAttr(opening, 'role'));
        const presentational = role === 'presentation' || role === 'none';
        const delegate = Boolean(getAttr(opening, 'data-click-delegate'));
        if (!presentational && !delegate && !(getAttr(opening, 'role') && getAttr(opening, 'tabIndex'))) {
          findings.clickable.push({ line, start: opening.start, end: opening.end, tag: name, element: el, path });
        }
      }
    },
    JSXAttribute(path) {
      if (path.node.name.name !== 'className') return;
      const frags = classFragments(path.node);
      const all = frags.map((f) => f.text).join(' ');
      if (/\banimate-(spin|pulse|bounce|ping)\b/.test(all)) findings.animate++;
      const hasRemoval = /(^|\s)(focus:|focus-visible:)?outline-none(\s|$)/.test(all);
      if (!hasRemoval) return;
      const replaced = /(^|\s)(focus|focus-visible|peer-focus|peer-focus-visible|group-focus):ring-/.test(all) || /(^|\s)focus(-visible)?:outline(?!-none)\b/.test(all);
      findings.outline.push({ line: path.node.loc?.start.line ?? 0, replaced, fragments: frags });
    },
  });
  return findings;
}
