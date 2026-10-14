"""Library title surface only; the existing native workbench owns shared chrome."""
LIBRARY_BACKGROUND = '#fbf8f1'


def apply(window, index):
    bg=LIBRARY_BACKGROUND
    # Navigation retains the native theme on every page. A library override here
    # previously changed the three already-approved workbenches as well.
    if window.sidebar_paper.styleSheet():
        window.sidebar_paper.setStyleSheet('')
    for name in ('titlebar_paper_left','titlebar_paper_center'):
        frame=getattr(window,name,None)
        if frame:
            library_canvas=index==2 and name!='titlebar_paper_left'
            style = f'background:{bg};border:0;' if library_canvas else ''
            if frame.styleSheet() != style:
                frame.setStyleSheet(style)
