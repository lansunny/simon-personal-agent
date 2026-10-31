/* Simon界面图标。 */
const {createApp,nextTick}=Vue;
const ICON_PATHS={
 'arrow-up-right':'<path d="M7 17 17 7M7 7h10v10"/>','arrow-right':'<path d="M5 12h14m-5-5 5 5-5 5"/>','arrow-up':'<path d="M12 19V5m-6 6 6-6 6 6"/>',
 'chevron-right':'<path d="m9 6 6 6-6 6"/>','check':'<path d="m5 12 4 4L19 6"/>','close':'<path d="m6 6 12 12M18 6 6 18"/>','plus':'<path d="M12 5v14M5 12h14"/>',
 'calendar':'<rect x="4" y="5" width="16" height="16" rx="3"/><path d="M8 3v4m8-4v4M4 10h16m-12 4h3m2 3h3"/>',
 'sun':'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
 'moon':'<path d="M20 15.5A8.5 8.5 0 0 1 8.5 4a8.5 8.5 0 1 0 11.5 11.5Z"/>',
 'person':'<circle cx="12" cy="8" r="3.2"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/>',
 'today':'<path d="M4 12a8 8 0 1 1 16 0M2 16h20m-16 4h12M12 2v2M3 5l2 2m16-2-2 2"/>',
 'chat':'<path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5H6l-4 3V10.5A7.5 7.5 0 0 1 9.5 3h3A7.5 7.5 0 0 1 20 10.5Z"/><path d="M7 10h8m-8 4h5"/>',
 'chart':'<path d="M4 4v16h17M9 15V9m5 6V5m5 10v-4"/>',
 'walk':'<circle cx="14" cy="4" r="2"/><path d="m8 13 3-5 4 2 3 4m-7-6 1 7 4 6m-4-6-5 6m1-8-4 2"/>',
 'tree':'<path d="m12 3-5 6h3l-6 7h16l-6-7h3L12 3Zm0 13v6m-4 0h8"/>',
 'leaf':'<path d="M20 3s2 13-5 16c-4 2-9 0-10-4C3 8 10 4 20 3Z"/><path d="M3 22 15 10"/>',
 'link':'<path d="m10 14 4-4m-5 6-2 2a4 4 0 0 1-5-5l5-5a4 4 0 0 1 5 0m3 0 2-2a4 4 0 0 1 5 5l-5 5a4 4 0 0 1-5 0"/>',
 'info':'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.2"/>','more':'<circle cx="5" cy="12" r=".7"/><circle cx="12" cy="12" r=".7"/><circle cx="19" cy="12" r=".7"/>',
 'pin':'<path d="M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 0 1 14 0Z"/><circle cx="12" cy="10" r="2"/>',
 'bell':'<path d="M5 17h14l-2-3V9A5 5 0 0 0 7 9v5l-2 3Zm5 3h4M12 2v2"/>',
 'sliders':'<path d="M4 7h3m4 0h9M4 17h9m4 0h3"/><circle cx="9" cy="7" r="2"/><circle cx="15" cy="17" r="2"/>',
 'reset':'<path d="M4 5v5h5m-5 0a8 8 0 1 1 1 8"/>','external':'<path d="M14 4h6v6m0-6L10 14M10 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5"/>'
};
