#!/usr/bin/env bash
# provision-secrets.sh -- RUNTIME create-if-absent engine for every internally generated
# Secret of the waddlebot chart (templates/provision-secrets.yaml mounts this file and the
# rendered spec into a pre-install/pre-upgrade hook Job; scripts/ci/tests/ drives it against
# a fake kubectl).
#
# Why runtime and not Helm template-time `lookup`: `lookup` is empty under
# `helm template | kubectl apply` / ArgoCD, so a template-time generator would regenerate
# EVERY auto value (all DB passwords, the root KEK) on each sync. Here the cluster itself is
# asked ("does this Secret/key already hold a real value?") at hook time, in every tier.
#
# Policy (never weakened):
#   * An existing non-placeholder value is NEVER overwritten or rotated -- not by an upgrade,
#     not by a different explicit value in Helm values (that is logged as a warning, no values).
#   * A key is "unset" only when absent, empty, or a REPLACE_ME* placeholder, and only for the
#     keys listed in the spec (the chart-owned keys). Other keys of the same Secret are never
#     touched; waddlebot-platform-credentials is never in the spec.
#   * unset key: explicit operator value (/inputs) > validated import from a NAMED legacy
#     Secret > fresh CSPRNG value (OpenSSL `rand`, whose DRBG is seeded from the OS CSPRNG
#     /dev/urandom/getrandom; sizes below are bits of raw entropy before encoding).
#   * Concurrency: a missing Secret is written with `kubectl create` (AlreadyExists => another
#     run won, re-read and adopt its values); an existing one is changed with a merge patch
#     carrying metadata.resourceVersion (Conflict => re-read and retry). Two runs can never
#     leave two different values behind.
#   * Values travel only through bash variables, tmpfs files ($WORK) and stdin/--patch-file --
#     never argv, never stdout/stderr, never `set -x`. Only Secret names and KEY NAMES are
#     logged. kubectl stderr is truncated and only printed on failure.
#
# Spec (pipe-separated, one directive per line, '#' comments):
#   secret|NAME|[skip=REASON]        begin a Secret block (skip = externally owned; not touched)
#   key|KEY|gen|ALG:ARG|OPTS         generated if unset. ALG: b64url:<bytes> alnum:<chars>
#                                    hex:<bytes> ring:<kid> (=> "<kid>:<64 hex>").
#                                    OPTS (';'-separated): min=N urlsafe hex64 strict-kept nodefaults
#                                    (reject placeholder-looking explicit/legacy values)
#                                    legacy=SECRET/KEY
#   key|KEY|derived|TEMPLATE|OPTS    recomputed every run from final values; %%KEY%% (same
#                                    Secret) or %%SECRET/KEY%% markers. OPTS: urlsafe-values
#   key|KEY|static|                  value from $INPUTS_DIR/<secret>__<KEY> when non-empty,
#                                    else created empty if absent (never blanks a real value)
#   key|KEY|blank|                   forced empty (scrubs a legacy leaked value)
#   marker|KEY|root-hmac-v1          stamp annotation waddles.penguintech.io/root-marker =
#                                    v1.<kek_id>.<hmac> from the 64-hex KEY (root-of-trust
#                                    guard for hub-api); a present-but-different marker FAILS.
set -euo pipefail
set +x
umask 077

NAMESPACE="${NAMESPACE:?NAMESPACE is required}"
SPEC_FILE="${SPEC_FILE:-/spec/spec.psv}"
INPUTS_DIR="${INPUTS_DIR:-/inputs}"
KUBECTL="${KUBECTL:-kubectl}"
MAX_TRIES="${MAX_TRIES:-6}"
LABEL_RELEASE="${LABEL_RELEASE:-waddlebot}"
DEPLOYMENT_TIER="${DEPLOYMENT_TIER:-alpha}"

KEEP_ANN="helm.sh/resource-policy"
MARKER_ANN="waddles.penguintech.io/root-marker"
CERT_ID_ANN="waddlebot.io/cert-identity"
CERT_SANS_ANN="waddlebot.io/cert-sans-sha256"
MARKER_MSG="waddles-root-check"
URLSAFE_RE='^[A-Za-z0-9._~-]+$'

WORK="$(mktemp -d "${WORK_PARENT:-${TMPDIR:-/tmp}}/provision-XXXXXX")"
cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT

N_EXAMINED=0; N_CREATED=0; N_PATCHED=0; N_UNCHANGED=0; N_SKIPPED=0; N_KEYS_ADDED=0
F_EXISTS=0; F_RV=""; F_KEEP=""; F_MARK=""; F_CID=""; F_CSAN=""

log() { printf '%s\n' "$*"; }
die() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }
kc() { "${KUBECTL}" -n "${NAMESPACE}" "$@"; }
b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
unb64() { printf '%s' "$1" | base64 -d; }
errhead() { local e; e="$(cat "$1" 2>/dev/null || true)"; printf '%s' "${e:0:300}"; }

# ---------------------------------------------------------------- CSPRNG ----
rand_b64url() { # <bytes> -> base64url without padding, never starting with '-'
  local n="$1" v want=$(( ($1 * 4 + 2) / 3 ))
  while :; do
    v="$(openssl rand -base64 "${n}" | tr -d '\n=' | tr '+/' '-_')" || return 1
    [ "${#v}" -ge "${want}" ] || return 1
    case "${v}" in -*) continue ;; esac
    printf '%s' "${v}"; return 0
  done
}
rand_hex() { # <bytes> -> lowercase hex
  local v
  v="$(openssl rand -hex "$1")" || return 1
  [ "${#v}" -eq $(( $1 * 2 )) ] || return 1
  printf '%s' "${v}"
}
rand_alnum() { # <chars> -> [A-Za-z0-9]; rejection-filtered, so unbiased
  local n="$1" v="" chunk
  while [ "${#v}" -lt "${n}" ]; do
    chunk="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9')" || return 1
    v="${v}${chunk}"
  done
  printf '%s' "${v:0:${n}}"
}
generate() { # <alg> <arg>
  case "$1" in
    b64url) rand_b64url "$2" ;;
    hex) rand_hex "$2" ;;
    alnum) rand_alnum "$2" ;;
    ring) local h; h="$(rand_hex 32)" || return 1; printf '%s:%s' "$2" "${h}" ;;
    *) return 1 ;;
  esac
}

# ------------------------------------------------- root marker (pure bash) ----
# HMAC-SHA256 with the key and message fed through stdin only (never argv): K is 32 bytes so
# the key block is K || 0x00*32; verified against python hmac in the unit test.
hmac_sha256_hex() { # <key hex 64> <message> -> hex
  local khex="$1" msg="$2" i b ip="" op="" inner
  for ((i = 0; i < 64; i++)); do
    if [ "${i}" -lt 32 ]; then b=$((16#${khex:$((i * 2)):2})); else b=0; fi
    ip="${ip}$(printf '\\x%02x' $((b ^ 54)))"
    op="${op}$(printf '\\x%02x' $((b ^ 92)))"
  done
  inner="$({ printf "${ip}"; printf '%s' "${msg}"; } | openssl dgst -sha256 -binary | base64 | tr -d '\n')"
  { printf "${op}"; printf '%s' "${inner}" | base64 -d; } | openssl dgst -sha256 -r | { read -r h _; printf '%s' "${h}"; }
}
kek_id_hex() { # <key hex> -> first 8 bytes of SHA-256(key) as hex (== hub_api envelope.crypto.kek_id)
  local khex="$1" i esc="" h
  for ((i = 0; i < ${#khex}; i += 2)); do esc="${esc}\\x${khex:${i}:2}"; done
  h="$(printf "${esc}" | openssl dgst -sha256 -r | { read -r x _; printf '%s' "${x}"; })"
  printf '%s' "${h:0:16}"
}
marker_value() { # <key hex> -> v1.<kek_id>.<hmac>
  local khex="$1" kid mac
  [[ "${khex}" =~ ^[0-9a-f]{64}$ ]] || return 1
  kid="$(kek_id_hex "${khex}")" || return 1
  mac="$(hmac_sha256_hex "${khex}" "${MARKER_MSG}")" || return 1
  printf 'v1.%s.%s' "${kid}" "${mac}"
}

# ------------------------------------------------------------- validation ----
is_real() { # non-empty and not a REPLACE_ME* placeholder
  [ -n "$1" ] && [[ "$1" != REPLACE_ME* ]]
}
has_opt() { [[ ";$1;" == *";$2;"* ]]; }
opt_val() { # <opts> <name> -> value of name=value
  local o=";$1;" rest
  [[ "${o}" == *";$2="* ]] || return 1
  rest="${o#*;"$2"=}"; printf '%s' "${rest%%;*}"
}
weak_default() { # known placeholder/default fragments (case-insensitive)
  local l; l="$(printf '%s' "$1" | tr 'A-Z' 'a-z')"
  [[ "${l}" == *changeme* || "${l}" == *change_me* || "${l}" == *replace_me* \
    || "${l}" == *example* || "${l}" == *password* || "${l}" == *dev_* || "${l}" == *change-in-production* ]]
}
check_value() { # <secret> <key> <value> <opts> <source: kept|generated|explicit|legacy>
  local s="$1" k="$2" v="$3" opts="$4" src="$5" min
  min="$(opt_val "${opts}" min || true)"; min="${min:-16}"
  if [ "${src}" != kept ] || has_opt "${opts}" strict-kept; then
    [ "${#v}" -ge "${min}" ] || die "secret/${s} key ${k}: ${src} value is shorter than ${min} characters"
  fi
  if has_opt "${opts}" hex64 && ! [[ "${v}" =~ ^[0-9a-f]{64}$ ]]; then
    die "secret/${s} key ${k}: ${src} value must be exactly 64 lowercase hex characters (256-bit key)"
  fi
  if has_opt "${opts}" urlsafe && { [ "${src}" != kept ] || has_opt "${opts}" strict-kept; } && ! [[ "${v}" =~ ${URLSAFE_RE} ]]; then
    die "secret/${s} key ${k}: ${src} value must match [A-Za-z0-9._~-] (it is embedded in a URL)"
  fi
  if { [ "${src}" = explicit ] || [ "${src}" = legacy ]; } && has_opt "${opts}" nodefaults && weak_default "${v}"; then
    die "secret/${s} key ${k}: ${src} value looks like a placeholder/default -- refusing it"
  fi
}

# ------------------------------------------------------ cluster reads ----
fetch_secret() { # <name> <dest dir> -> sets F_EXISTS F_RV F_KEEP F_MARK F_CID F_CSAN, writes <dest>/<KEY> (base64)
  local name="$1" dest="$2" out rc tag a b err="${WORK}/err"
  rm -rf "${dest}"; mkdir -p "${dest}"
  F_EXISTS=0; F_RV=""; F_KEEP=""; F_MARK=""; F_CID=""; F_CSAN=""
  set +e
  out="$(kc get secret "${name}" -o go-template='RV {{.metadata.resourceVersion}}{{"\n"}}ANN {{with .metadata.annotations}}{{index . "'"${KEEP_ANN}"'"}}{{end}}{{"\n"}}MARK {{with .metadata.annotations}}{{index . "'"${MARKER_ANN}"'"}}{{end}}{{"\n"}}CID {{with .metadata.annotations}}{{index . "'"${CERT_ID_ANN}"'"}}{{end}}{{"\n"}}CSAN {{with .metadata.annotations}}{{index . "'"${CERT_SANS_ANN}"'"}}{{end}}{{"\n"}}{{with .data}}{{range $k, $v := .}}K {{$k}} {{$v}}{{"\n"}}{{end}}{{end}}' 2>"${err}")"
  rc=$?
  set -e
  if [ "${rc}" -ne 0 ]; then
    if [[ "$(cat "${err}")" == *NotFound* ]]; then return 0; fi
    die "kubectl get secret/${name} failed: $(errhead "${err}")"
  fi
  F_EXISTS=1
  # kubectl's go-template prints the literal "<no value>" for a missing key inside a NON-empty
  # annotations map (a nil map skips `with`, hence ""). Treat it as absent, or an existing Secret
  # that has other annotations but no marker/cert annotation looks like a mismatching stamp.
  out="${out//'<no value>'/}"
  while IFS=' ' read -r tag a b; do
    case "${tag}" in
      RV) F_RV="${a}" ;;
      ANN) F_KEEP="${a}" ;;
      MARK) F_MARK="${a}" ;;
      CID) F_CID="${a}" ;;
      CSAN) F_CSAN="${a}" ;;
      K) printf '%s' "${b}" > "${dest}/${a}" ;;
    esac
  done <<EOF
${out}
EOF
}
cur_b64() { # <key> in the Secret currently being processed; empty when absent
  [ -f "${WORK}/cur/$1" ] && cat "${WORK}/cur/$1" || true
}
cur_has() { [ -f "${WORK}/cur/$1" ]; }

legacy_value() { # <SECRET/KEY> -> decoded value of a NAMED Secret, empty if absent (never env/ConfigMap)
  local sn="${1%%/*}" sk="${1#*/}" e="${F_EXISTS}" r="${F_RV}" k="${F_KEEP}" m="${F_MARK}" v=""
  fetch_secret "${sn}" "${WORK}/leg"
  if [ -f "${WORK}/leg/${sk}" ]; then v="$(unb64 "$(cat "${WORK}/leg/${sk}")")"; fi
  F_EXISTS="${e}"; F_RV="${r}"; F_KEEP="${k}"; F_MARK="${m}"
  printf '%s' "${v}"
}

final_b64() { # <secret> <key> -> base64 of the final value (this run's block, or a loaded other Secret)
  local sn="$1" sk="$2" f
  if [ "${sn}" = "${CUR_NAME}" ]; then
    f="${WORK}/fin/${sk}"
  else
    if [ ! -d "${WORK}/final/${sn}" ]; then
      local e="${F_EXISTS}" r="${F_RV}" k="${F_KEEP}" m="${F_MARK}"
      fetch_secret "${sn}" "${WORK}/final/${sn}"
      [ "${F_EXISTS}" = 1 ] || die "referenced secret/${sn} does not exist (needed for ${CUR_NAME}/${sk})"
      F_EXISTS="${e}"; F_RV="${r}"; F_KEEP="${k}"; F_MARK="${m}"
    fi
    f="${WORK}/final/${sn}/${sk}"
  fi
  [ -s "${f}" ] || die "referenced key ${sn}/${sk} is missing or empty (needed for ${CUR_NAME})"
  cat "${f}"
}

subst() { # <template> <flags> -> template with %%markers%% replaced
  local t="$1" flags="$2" out="" pre rest ref sn sk v
  while [[ "${t}" == *'%%'* ]]; do
    pre="${t%%'%%'*}"; rest="${t#*'%%'}"
    [[ "${rest}" == *'%%'* ]] || die "unterminated %% marker in a derived template of secret/${CUR_NAME}"
    ref="${rest%%'%%'*}"; t="${rest#*'%%'}"
    if [[ "${ref}" == */* ]]; then sn="${ref%%/*}"; sk="${ref#*/}"; else sn="${CUR_NAME}"; sk="${ref}"; fi
    v="$(unb64 "$(final_b64 "${sn}" "${sk}")")"
    if [ "${flags}" = urlsafe-values ] && ! [[ "${v}" =~ ${URLSAFE_RE} ]]; then
      die "value of ${sn}/${sk} is not URL/JSON safe [A-Za-z0-9._~-] (needed for ${CUR_NAME})"
    fi
    out="${out}${pre}${v}"
  done
  printf '%s' "${out}${t}"
}

# ------------------------------------------------------- block resolution ----
note_new() { # <key> <final b64>
  printf '%s' "$2" > "${WORK}/new/$1"
  N_BLOCK_ADDED=$((N_BLOCK_ADDED + 1))
}

# ------------------------------------------------------- ECDSA P-256 TLS ----
gen_p256_ca() { # <ca_dir> <cn> <days>
  local dir="$1" cn="$2" days="${3:-365}"
  mkdir -p "${dir}"
  [ -s "${dir}/ca.key" ] && [ -s "${dir}/ca.crt" ] && return 0
  openssl ecparam -name prime256v1 -genkey -noout -out "${dir}/ca.key"
  cat > "${dir}/ca.cnf" <<EOF
[req]
prompt = no
distinguished_name = dn
x509_extensions = v3_ca
[dn]
CN = ${cn}
[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
EOF
  openssl req -x509 -new -sha256 -key "${dir}/ca.key" -days "${days}" -config "${dir}/ca.cnf" -out "${dir}/ca.crt" 2>/dev/null
}

gen_p256_leaf() { # <ca_dir> <out_crt> <out_key> <cn> <dns_csv> <uri_csv> <days>
  local ca_dir="$1" out_crt="$2" out_key="$3" cn="$4" dns_csv="$5" uri_csv="$6" days="${7:-365}"
  local san="" item serial
  local IFS=','
  for item in ${dns_csv}; do
    [ -n "${item}" ] || continue
    san="${san:+${san},}DNS:${item}"
  done
  for item in ${uri_csv}; do
    [ -n "${item}" ] || continue
    san="${san:+${san},}URI:${item}"
  done
  unset IFS
  openssl ecparam -name prime256v1 -genkey -noout -out "${out_key}"
  cat > "${ca_dir}/leaf_req.cnf" <<EOF
[req]
prompt = no
distinguished_name = dn
[dn]
CN = ${cn}
EOF
  openssl req -new -sha256 -key "${out_key}" -config "${ca_dir}/leaf_req.cnf" -out "${ca_dir}/leaf.csr" 2>/dev/null
  cat > "${ca_dir}/leaf_ext.cnf" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
${san:+subjectAltName=${san}}
EOF
  serial="$(openssl rand -hex 16)"
  openssl x509 -req -sha256 -in "${ca_dir}/leaf.csr" -CA "${ca_dir}/ca.crt" -CAkey "${ca_dir}/ca.key" \
    -set_serial "0x${serial}" -days "${days}" -extfile "${ca_dir}/leaf_ext.cnf" -out "${out_crt}" 2>/dev/null
}

tls_block_kept() { # <blk> -> returns 0 if existing secret is complete & matches CERT_ID/CERT_SANS
  [ "${F_EXISTS}" = 1 ] || return 1
  [ -z "${CERT_ID}" ] || [ "${F_CID}" = "${CERT_ID}" ] || return 1
  [ -z "${CERT_SANS}" ] || [ "${F_CSAN}" = "${CERT_SANS}" ] || return 1
  local kind a b c d e f g
  while IFS='|' read -r -u 4 kind a b c d e f g; do
    case "${kind}" in
      tls_ca) [ -n "$(cur_b64 "${b}")" ] || return 1 ;;
      tls_leaf) [ -n "$(cur_b64 "${b}")" ] && [ -n "$(cur_b64 "${c}")" ] || return 1 ;;
      tls_alias) [ -n "$(cur_b64 "${a}")" ] || return 1 ;;
      tls_concat) [ -n "$(cur_b64 "${a}")" ] || return 1 ;;
    esac
  done 4< "$1"
  return 0
}

resolve_block() { # <block file> ; fills ${WORK}/fin (all final values) and ${WORK}/new (changes)
  local blk="$1" kind key how arg opts cur v fin src tries legacy_ref dec inp
  local tls_kept=0 tls_init=0 ca_dir ca_tag ca_key ca_cn days cert_key key_key cn dns_csv uri_csv ref_sec ref_k joined part
  : > "${WORK}/keys"; MARKER_KEY=""; CERT_ID=""; CERT_SANS=""
  while IFS='|' read -r -u 3 kind key how arg opts; do
    case "${kind}" in
      secret) continue ;;
      marker) MARKER_KEY="${key}"; continue ;;
      tls_meta) CERT_ID="${key}"; CERT_SANS="${how}"; continue ;;
      tls_ca|tls_leaf|tls_alias|tls_concat)
        if [ "${tls_init}" -eq 0 ]; then
          tls_init=1
          if tls_block_kept "${blk}"; then tls_kept=1; fi
        fi
        case "${kind}" in
          tls_ca)
            ca_tag="${key}"; ca_key="${how}"; ca_cn="${arg}"; days="${opts:-365}"
            printf '%s\n' "${ca_key}" >> "${WORK}/keys"
            cur="$(cur_b64 "${ca_key}")"
            inp="${INPUTS_DIR}/${CUR_NAME}__${ca_key}"
            if [ -s "${inp}" ]; then
              fin="$(b64 "$(cat "${inp}")")"
              [ "${fin}" = "${cur}" ] || note_new "${ca_key}" "${fin}"
            elif [ "${tls_kept}" -eq 1 ]; then
              fin="${cur}"
            else
              case "${DEPLOYMENT_TIER}" in
                alpha|local) ;;
                *) die "secret/${CUR_NAME}: key ${ca_key} not found and DEPLOYMENT_TIER=${DEPLOYMENT_TIER} is outside alpha/local -- auto-generation is alpha/local only" ;;
              esac
              ca_dir="${WORK}/tls_ca/${ca_tag}"
              gen_p256_ca "${ca_dir}" "${ca_cn}" "${days}" || die "secret/${CUR_NAME}: P-256 CA generation failed"
              fin="$(base64 < "${ca_dir}/ca.crt" | tr -d '\n')"
              note_new "${ca_key}" "${fin}"
              log "unset -> generated(p256-ca): secret/${CUR_NAME} key ${ca_key}"
            fi
            printf '%s' "${fin}" > "${WORK}/fin/${ca_key}"
            ;;
          tls_leaf)
            IFS='|' read -r _ ca_tag cert_key key_key cn dns_csv uri_csv days <<< "${kind}|${key}|${how}|${arg}|${opts}"
            printf '%s\n%s\n' "${cert_key}" "${key_key}" >> "${WORK}/keys"
            cur="$(cur_b64 "${cert_key}")"
            local cur_k="$(cur_b64 "${key_key}")"
            local inp_c="${INPUTS_DIR}/${CUR_NAME}__${cert_key}"
            local inp_k="${INPUTS_DIR}/${CUR_NAME}__${key_key}"
            if [ -s "${inp_c}" ] && [ -s "${inp_k}" ]; then
              local fin_c fin_k
              fin_c="$(b64 "$(cat "${inp_c}")")"; fin_k="$(b64 "$(cat "${inp_k}")")"
              [ "${fin_c}" = "${cur}" ] || note_new "${cert_key}" "${fin_c}"
              [ "${fin_k}" = "${cur_k}" ] || note_new "${key_key}" "${fin_k}"
              printf '%s' "${fin_c}" > "${WORK}/fin/${cert_key}"
              printf '%s' "${fin_k}" > "${WORK}/fin/${key_key}"
            elif [ "${tls_kept}" -eq 1 ]; then
              printf '%s' "${cur}" > "${WORK}/fin/${cert_key}"
              printf '%s' "${cur_k}" > "${WORK}/fin/${key_key}"
            else
              case "${DEPLOYMENT_TIER}" in
                alpha|local) ;;
                *) die "secret/${CUR_NAME}: keys ${cert_key},${key_key} not found and DEPLOYMENT_TIER=${DEPLOYMENT_TIER} is outside alpha/local -- auto-generation is alpha/local only" ;;
              esac
              ca_dir="${WORK}/tls_ca/${ca_tag}"
              gen_p256_ca "${ca_dir}" "${ca_tag}" "${days:-365}" || die "secret/${CUR_NAME}: P-256 CA init failed"
              local tmp_leaf="${WORK}/tls_leaf_${CUR_NAME}_${cert_key}"
              gen_p256_leaf "${ca_dir}" "${tmp_leaf}.crt" "${tmp_leaf}.key" "${cn}" "${dns_csv}" "${uri_csv}" "${days:-365}" \
                || die "secret/${CUR_NAME}: P-256 leaf generation failed for ${cert_key}"
              local fin_c fin_k
              fin_c="$(base64 < "${tmp_leaf}.crt" | tr -d '\n')"
              fin_k="$(base64 < "${tmp_leaf}.key" | tr -d '\n')"
              rm -f "${tmp_leaf}.crt" "${tmp_leaf}.key"
              note_new "${cert_key}" "${fin_c}"
              note_new "${key_key}" "${fin_k}"
              printf '%s' "${fin_c}" > "${WORK}/fin/${cert_key}"
              printf '%s' "${fin_k}" > "${WORK}/fin/${key_key}"
              log "unset -> generated(p256-leaf): secret/${CUR_NAME} keys ${cert_key},${key_key}"
            fi
            ;;
          tls_alias)
            printf '%s\n' "${key}" >> "${WORK}/keys"
            cur="$(cur_b64 "${key}")"
            fin="$(cat "${WORK}/fin/${how}")"
            [ "${fin}" = "${cur}" ] || note_new "${key}" "${fin}"
            printf '%s' "${fin}" > "${WORK}/fin/${key}"
            ;;
          tls_concat)
            printf '%s\n' "${key}" >> "${WORK}/keys"
            cur="$(cur_b64 "${key}")"
            joined=""
            local IFS=','
            for part in ${how}; do
              [ -n "${part}" ] || continue
              ref_sec="${part%%/*}"; ref_k="${part#*/}"
              [ -f "${WORK}/final/${ref_sec}/${ref_k}" ] || die "secret/${CUR_NAME} key ${key}: missing ${ref_sec}/${ref_k}"
              dec="$(unb64 "$(cat "${WORK}/final/${ref_sec}/${ref_k}")")"
              joined="${joined:+${joined}$'\n'}${dec}"
            done
            unset IFS
            fin="$(b64 "${joined}")"
            [ "${fin}" = "${cur}" ] || note_new "${key}" "${fin}"
            printf '%s' "${fin}" > "${WORK}/fin/${key}"
            ;;
        esac
        continue
        ;;
      key) ;;
      *) die "unknown spec directive '${kind}' in secret/${CUR_NAME}" ;;
    esac
    printf '%s\n' "${key}" >> "${WORK}/keys"
    cur="$(cur_b64 "${key}")"
    inp="${INPUTS_DIR}/${CUR_NAME}__${key}"
    case "${how}" in
      gen)
        dec=""; [ -z "${cur}" ] || dec="$(unb64 "${cur}")"
        if [ "${F_EXISTS}" = 1 ] && is_real "${dec}"; then
          fin="${cur}"
          check_value "${CUR_NAME}" "${key}" "${dec}" "${opts}" kept
          if [ -s "${inp}" ] && [ "$(b64 "$(cat "${inp}")")" != "${cur}" ]; then
            log "warn: explicit value for secret/${CUR_NAME} key ${key} ignored -- the Secret already holds a different real value; upgrades never overwrite or rotate it (delete that key to re-seed)"
          fi
        else
          src=""; v=""
          if [ -s "${inp}" ]; then
            v="$(cat "${inp}")"
            is_real "${v}" || die "secret/${CUR_NAME} key ${key}: explicit value is a REPLACE_ME placeholder"
            src=explicit
          else
            legacy_ref="$(opt_val "${opts}" legacy || true)"
            if [ -n "${legacy_ref}" ]; then
              v="$(legacy_value "${legacy_ref}")"
              if is_real "${v}"; then src=legacy; else v=""; fi
            fi
            if [ -z "${src}" ]; then
              tries=0
              while :; do
                v="$(generate "${arg%%:*}" "${arg#*:}")" || die "secret/${CUR_NAME} key ${key}: CSPRNG generation failed (alg ${arg%%:*})"
                tries=$((tries + 1))
                if ! weak_default "${v}" || [ "${tries}" -ge 20 ]; then break; fi
              done
              src=generated
            fi
          fi
          check_value "${CUR_NAME}" "${key}" "${v}" "${opts}" "${src}"
          fin="$(b64 "${v}")"
          note_new "${key}" "${fin}"
          log "unset -> ${src}: secret/${CUR_NAME} key ${key}"
        fi
        ;;
      derived)
        v="$(subst "${arg}" "${opts}")"
        fin="$(b64 "${v}")"
        if [ "${fin}" != "${cur}" ]; then note_new "${key}" "${fin}"; fi
        ;;
      static)
        if [ -s "${inp}" ]; then
          fin="$(b64 "$(cat "${inp}")")"
          if [ "${fin}" != "${cur}" ]; then note_new "${key}" "${fin}"; fi
        elif cur_has "${key}"; then
          fin="${cur}"
        else
          fin=""; note_new "${key}" ""
        fi
        ;;
      blank)
        fin=""
        if [ -n "${cur}" ]; then note_new "${key}" ""; fi
        ;;
      *) die "unknown key mode '${how}' for secret/${CUR_NAME} key ${key}" ;;
    esac
    printf '%s' "${fin}" > "${WORK}/fin/${key}"
  done 3< "${blk}"
  [ -s "${WORK}/keys" ] || die "secret/${CUR_NAME}: spec lists no keys"
}

# ---------------------------------------------------------------- writing ----
json_data() { # all final values -> "K":"b64",...
  local k first=1 out=""
  while IFS= read -r k; do
    [ "${first}" = 1 ] || out="${out},"
    first=0
    out="${out}\"${k}\":\"$(cat "${WORK}/fin/${k}")\""
  done < "${WORK}/keys"
  printf '%s' "${out}"
}
json_new_data() { # only changed keys
  local k first=1 out=""
  while IFS= read -r k; do
    [ -f "${WORK}/new/${k}" ] || continue
    [ "${first}" = 1 ] || out="${out},"
    first=0
    out="${out}\"${k}\":\"$(cat "${WORK}/new/${k}")\""
  done < "${WORK}/keys"
  printf '%s' "${out}"
}
keep_annotations_json() {
  printf '"%s":"keep","argocd.argoproj.io/compare-options":"IgnoreExtraneous","argocd.argoproj.io/sync-options":"Prune=false","waddles.penguintech.io/provisioned-by":"provision-secrets"' "${KEEP_ANN}"
}
write_secret() { # returns 0 done, 1 retry (lost a race)
  local name="$1" marker="" ann="" err="${WORK}/werr" rc
  if [ -n "${MARKER_KEY}" ]; then
    marker="$(marker_value "$(unb64 "$(cat "${WORK}/fin/${MARKER_KEY}")")")" \
      || die "secret/${name} key ${MARKER_KEY} is not 64 lowercase hex chars -- cannot stamp the root marker"
    if [ "${F_EXISTS}" = 1 ] && [ -n "${F_MARK}" ] && [ "${F_MARK}" != "${marker}" ]; then
      die "root marker mismatch on secret/${name}: ${MARKER_KEY} no longer matches the root it was stamped for (annotation ${MARKER_ANN}). Refusing to continue -- restore the original key, or an operator must consciously remove the annotation after re-sealing dependent data"
    fi
  fi
  if [ "${F_EXISTS}" = 0 ]; then
    ann="$(keep_annotations_json)"
    [ -z "${marker}" ] || ann="${ann},\"${MARKER_ANN}\":\"${marker}\""
    [ -z "${CERT_ID}" ] || ann="${ann},\"${CERT_ID_ANN}\":\"${CERT_ID}\""
    [ -z "${CERT_SANS}" ] || ann="${ann},\"${CERT_SANS_ANN}\":\"${CERT_SANS}\""
    printf '{"apiVersion":"v1","kind":"Secret","metadata":{"name":"%s","namespace":"%s","labels":{"app.kubernetes.io/part-of":"waddlebot","app.kubernetes.io/managed-by":"waddlebot-provision-secrets","waddles.penguintech.io/release":"%s"},"annotations":{%s}},"type":"Opaque","data":{%s}}' \
      "${name}" "${NAMESPACE}" "${LABEL_RELEASE}" "${ann}" "$(json_data)" > "${WORK}/create.json"
    set +e; kc create -f "${WORK}/create.json" >/dev/null 2>"${err}"; rc=$?; set -e
    rm -f "${WORK}/create.json"
    if [ "${rc}" -eq 0 ]; then
      N_CREATED=$((N_CREATED + 1)); N_KEYS_ADDED=$((N_KEYS_ADDED + N_BLOCK_ADDED))
      log "created: secret/${name}"
      return 0
    fi
    if [[ "$(cat "${err}")" == *AlreadyExists* ]]; then
      log "race: secret/${name} was created concurrently -- adopting the winner's values"
      return 1
    fi
    die "kubectl create secret/${name} failed: $(errhead "${err}")"
  fi
  # existing Secret: patch ONLY changed keys (+ keep/argocd/marker/cert annotations when missing)
  [ "${F_KEEP}" = keep ] || ann="$(keep_annotations_json)"
  if [ -n "${marker}" ] && [ -z "${F_MARK}" ]; then
    [ -z "${ann}" ] || ann="${ann},"
    ann="${ann}\"${MARKER_ANN}\":\"${marker}\""
  fi
  if [ -n "${CERT_ID}" ] && [ "${F_CID}" != "${CERT_ID}" ]; then
    [ -z "${ann}" ] || ann="${ann},"
    ann="${ann}\"${CERT_ID_ANN}\":\"${CERT_ID}\""
  fi
  if [ -n "${CERT_SANS}" ] && [ "${F_CSAN}" != "${CERT_SANS}" ]; then
    [ -z "${ann}" ] || ann="${ann},"
    ann="${ann}\"${CERT_SANS_ANN}\":\"${CERT_SANS}\""
  fi
  if [ "${N_BLOCK_ADDED}" -eq 0 ] && [ -z "${ann}" ]; then
    N_UNCHANGED=$((N_UNCHANGED + 1))
    log "unchanged: secret/${name} (all owned keys already hold real values)"
    return 0
  fi
  {
    printf '{"metadata":{"resourceVersion":"%s"' "${F_RV}"
    [ -z "${ann}" ] || printf ',"annotations":{%s}' "${ann}"
    printf '}'
    [ "${N_BLOCK_ADDED}" -eq 0 ] || printf ',"data":{%s}' "$(json_new_data)"
    printf '}'
  } > "${WORK}/patch.json"
  set +e; kc patch secret "${name}" --type=merge --patch-file "${WORK}/patch.json" >/dev/null 2>"${err}"; rc=$?; set -e
  rm -f "${WORK}/patch.json"
  if [ "${rc}" -eq 0 ]; then
    N_PATCHED=$((N_PATCHED + 1)); N_KEYS_ADDED=$((N_KEYS_ADDED + N_BLOCK_ADDED))
    log "patched: secret/${name} (${N_BLOCK_ADDED} key(s) set, existing real values untouched)"
    return 0
  fi
  if [[ "$(cat "${err}")" == *Conflict* || "$(cat "${err}")" == *NotFound* || "$(cat "${err}")" == *"has been modified"* ]]; then
    log "race: secret/${name} changed concurrently -- re-reading"
    return 1
  fi
  die "kubectl patch secret/${name} failed: $(errhead "${err}")"
}

process_secret() { # <block file>
  local blk="$1" flags attempt=0
  IFS='|' read -r _ CUR_NAME flags < "${blk}"
  N_EXAMINED=$((N_EXAMINED + 1))
  if [[ "${flags}" == skip=* ]]; then
    N_SKIPPED=$((N_SKIPPED + 1))
    log "skip: secret/${CUR_NAME} (${flags#skip=}) -- not touched"
    return 0
  fi
  while :; do
    attempt=$((attempt + 1))
    [ "${attempt}" -le "${MAX_TRIES}" ] || die "secret/${CUR_NAME}: gave up after ${MAX_TRIES} attempts (concurrent writers)"
    fetch_secret "${CUR_NAME}" "${WORK}/cur"
    rm -rf "${WORK}/new" "${WORK}/fin"; mkdir -p "${WORK}/new" "${WORK}/fin"
    N_BLOCK_ADDED=0
    resolve_block "${blk}"
    if write_secret "${CUR_NAME}"; then break; fi
  done
  rm -rf "${WORK}/final/${CUR_NAME}"; mkdir -p "${WORK}/final/${CUR_NAME}"
  cp -R "${WORK}/fin/." "${WORK}/final/${CUR_NAME}/"
}

main() {
  local n=0 blk="" line f
  [ -r "${SPEC_FILE}" ] || die "spec file ${SPEC_FILE} is not readable"
  mkdir -p "${WORK}/blocks" "${WORK}/final"
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in ''|'#'*) continue ;; esac
    case "${line}" in
      secret\|*) n=$((n + 1)); blk="${WORK}/blocks/$(printf '%04d' "${n}")"; printf '%s\n' "${line}" > "${blk}" ;;
      *) [ -n "${blk}" ] || die "spec directive before any 'secret|' line"; printf '%s\n' "${line}" >> "${blk}" ;;
    esac
  done < "${SPEC_FILE}"
  [ "${n}" -gt 0 ] || die "spec lists zero Secrets -- refusing to report success on an empty denominator"
  for f in "${WORK}/blocks"/*; do process_secret "${f}"; done
  log "provision-secrets: examined=${N_EXAMINED} created=${N_CREATED} patched=${N_PATCHED} unchanged=${N_UNCHANGED} skipped=${N_SKIPPED} keys_set=${N_KEYS_ADDED}"
  [ "${N_EXAMINED}" -eq "${n}" ] || die "examined ${N_EXAMINED} of ${n} Secrets"
}

main "$@"
