export default function Home() {
  return (
    <div className="flex items-center justify-center h-screen">
      <div className="text-center">
        <h1 className="text-4xl font-bold text-gray-900 mb-4">AI Write</h1>
        <p className="text-lg text-gray-600">AI-Powered Novel Writing Platform</p>
        <div className="mt-6 flex items-center justify-center gap-4">
          <a href="/workspace" className="inline-block px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700">
            Enter Workspace
          </a>
          <a href="/knowledge" className="inline-block px-6 py-3 bg-white text-blue-600 border border-blue-600 rounded-lg hover:bg-blue-50">
            Knowledge Base
          </a>
          <a href="/settings" className="inline-block px-6 py-3 bg-white text-gray-600 border border-gray-300 rounded-lg hover:bg-gray-50">
            Model Settings
          </a>
        </div>
      </div>
    </div>
  )
}
