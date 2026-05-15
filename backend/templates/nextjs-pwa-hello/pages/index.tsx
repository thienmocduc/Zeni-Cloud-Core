import Head from 'next/head';
import { useEffect, useState } from 'react';

export default function Home() {
  const [installable, setInstallable] = useState<any>(null);
  const [installed, setInstalled] = useState(false);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(() => {});
    }
    const onPrompt = (e: any) => {
      e.preventDefault();
      setInstallable(e);
    };
    const onInstalled = () => setInstalled(true);
    window.addEventListener('beforeinstallprompt', onPrompt);
    window.addEventListener('appinstalled', onInstalled);
    return () => {
      window.removeEventListener('beforeinstallprompt', onPrompt);
      window.removeEventListener('appinstalled', onInstalled);
    };
  }, []);

  const install = async () => {
    if (!installable) return;
    installable.prompt();
    const choice = await installable.userChoice;
    if (choice.outcome === 'accepted') setInstalled(true);
    setInstallable(null);
  };

  const features = [
    'manifest.webmanifest auto-served at /manifest.webmanifest',
    'Service worker registered at /sw.js (cache-first shell)',
    'Install button appears on supported browsers',
    'Offline fallback via cached app shell',
    'Standalone display mode — no browser chrome on mobile',
    'Edit pages/index.tsx + public/sw.js to customize'
  ];

  return (
    <>
      <Head>
        <title>Next.js PWA on Zeni Cloud</title>
        <link rel="manifest" href="/manifest.webmanifest" />
        <meta name="theme-color" content="#a855f7" />
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
      </Head>
      <main style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'linear-gradient(135deg,#0f0f23,#1a1a3e,#2d1b69)',
        color: '#fff',
        fontFamily: 'system-ui, -apple-system, sans-serif',
        padding: 24
      }}>
        <div style={{ maxWidth: 720, textAlign: 'center' }}>
          <div style={{ fontSize: 64, marginBottom: 12 }}>📱</div>
          <h1 style={{ fontSize: 36, color: '#a855f7', margin: 0 }}>
            Next.js PWA on Zeni Cloud
          </h1>
          <p style={{ color: '#cbd5e1', marginTop: 12 }}>
            Install this app to your home screen for an offline-capable experience.
          </p>
          {installed ? (
            <div style={{ marginTop: 16, padding: '10px 18px', background: 'rgba(34,197,94,0.15)', color: '#22c55e', borderRadius: 8, display: 'inline-block' }}>
              ✓ Installed — open from your home screen
            </div>
          ) : installable ? (
            <button onClick={install} style={{
              marginTop: 16, padding: '12px 24px',
              background: 'linear-gradient(90deg,#a855f7,#7d68ff)',
              color: '#fff', border: 0, borderRadius: 10,
              fontSize: 14, fontWeight: 700, cursor: 'pointer'
            }}>
              📥 Install this PWA
            </button>
          ) : (
            <p style={{ color: '#94a3b8', fontSize: 12, marginTop: 16 }}>
              On supported browsers, you'll see an Install button here. iOS Safari: tap Share → Add to Home Screen.
            </p>
          )}
          <ul style={{
            listStyle: 'none', textAlign: 'left',
            background: 'rgba(255,255,255,0.04)',
            border: '1px solid rgba(168,85,247,0.3)',
            borderRadius: 12, padding: 24, marginTop: 28
          }}>
            {features.map(f => (
              <li key={f} style={{ padding: '6px 0', color: '#e2e8f0' }}>
                <span style={{ color: '#22d3ee', marginRight: 8 }}>✓</span>{f}
              </li>
            ))}
          </ul>
          <p style={{ fontSize: 12, color: '#64748b', marginTop: 24 }}>
            Powered by <strong style={{ color: '#a855f7' }}>Zeni Cloud</strong> · Next.js 14 PWA
          </p>
        </div>
      </main>
    </>
  );
}
