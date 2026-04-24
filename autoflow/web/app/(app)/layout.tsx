import { Sidebar } from "@/components/app/sidebar";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex-1">
        <div className="mx-auto w-full max-w-7xl p-6 md:p-8">{children}</div>
      </div>
    </div>
  );
}
