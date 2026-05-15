import Head from 'next/head';

export default function Home() {
  const features = [
    'Server-side rendering (SSR) ready',
    'Standalone output for tiny Docker images',
    'Auto-scale on Zeni Cloud Run',
    'HTTPS + custom domain ready',
    'Edit pages/index.tsx and redeploy'
  ];
  return (
    <>
      <Head>
        <title>Welcome to Next.js on Zeni Cloud</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>
      <main style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'linear-gradient(135deg,#0f0f23,#1a1a3e)',
        color: '#fff',
        fontFamily: 'system-ui, -apple-system, sans-serif',
        padding: '24px'
      }}>
        <div style={{ maxWidth: 720, textAlign: 'center' }}>
          <div style={{ fontSize: 64, marginBottom: 16 }}>▲</div>
          <h1 style={{ fontSize: 40, margin: 0, color: '#a855f7' }}>
            Welcome to Next.js on Zeni Cloud
          </h1>
          <p style={{ fontSize: 16, color: '#cbd5e1', margin: '12px 0 28px' }}>
            Your Next.js 14 starter is live. Replace this page to ship your app.
          </p>
          <ul style={{
            listStyle: 'none', textAlign: 'left',
            background: 'rgba(255,255,255,0.04)',
            border: '1px solid rgba(168,85,247,0.3)',
            borderRadius: 12, padding: 24
          }}>
            {features.map(f => (
              <li key={f} style={{ padding: '8px 0', color: '#e2e8f0' }}>
                <span style={{ color: '#22d3ee', marginRight: 8 }}>✓</span>{f}
              </li>
            ))}
          </ul>
          <p style={{ fontSize: 12, color: '#64748b', marginTop: 32 }}>
            Powered by <strong style={{ color: '#a855f7' }}>Zeni Cloud</strong> · Next.js 14
          </p>
        </div>
      </main>
    </>
  );
}
