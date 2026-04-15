export default function Home() {
  return (
    <div className="flex items-center justify-center h-screen">
      <div className="text-center">
        <h1 className="text-4xl font-bold text-gray-900 mb-4">AI Write</h1>
        <p className="text-lg text-gray-600">AI-Powered Novel Writing Platform</p>
        <a href="/workspace" className="mt-6 inline-block px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700">
          Enter Workspace
        </a>
      </div>
    </div>
  )
}
