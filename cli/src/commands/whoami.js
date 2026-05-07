/**
 * zeni whoami — Show current login info (multi-project aware v0.2)
 */
import { readConfig, readProjectConfig, getToken, getWorkspace, getTokenByAlias, apiCall } from '../config.js';

export async function whoami() {
  const proj = readProjectConfig();
  const token = getToken();
  const workspace = getWorkspace();

  if (!token) {
    console.log('Chưa có token. Run: zeni login   (hoặc: zeni alias add <name>)');
    return;
  }

  console.log('\x1b[36m▸\x1b[0m Resolved context:');
  if (proj && proj.token_alias) {
    console.log('  source:        zeni.json (' + proj.__path + ')');
    console.log('  token_alias:   ' + proj.token_alias);
    const aliased = getTokenByAlias(proj.token_alias);
    if (aliased) {
      console.log('  alias_workspace:' + (aliased.workspace || '?'));
    }
  } else if (process.env.ZENI_TOKEN) {
    console.log('  source:        ZENI_TOKEN env var');
  } else {
    console.log('  source:        ~/.zeni/config.json (legacy default)');
  }
  console.log('  workspace:     ' + (workspace || '(none)'));
  console.log('  token:         ' + token.slice(0, 16) + '…');
  console.log();
  console.log('\x1b[36m▸\x1b[0m Verify với backend:');
  try {
    const me = await apiCall('GET', '/auth/me');
    console.log('  ✓ Email:       ' + me.email);
    console.log('  ✓ Role:        ' + me.role);
    console.log('  ✓ Workspaces:  ' + ((me.workspaces || []).join(', ') || '(none)'));
  } catch (e) {
    console.error('  ✗ Backend verify failed:', e.message);
  }
}
