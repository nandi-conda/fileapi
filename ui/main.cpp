#include <QApplication>

#include "appstyle.h"
#include "forgewindow.h"

int main(int argc, char *argv[])
{
    QApplication app(argc, argv);
    QApplication::setApplicationName("fileapi forge");
    QApplication::setStyle(new AppStyle(AppStyle::detectTouch()));
    ForgeWindow w;
    w.showFullScreen();  // fills the page, without a window frame
    return app.exec();
}
