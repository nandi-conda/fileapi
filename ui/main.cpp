#include <QApplication>
#include <QStyleFactory>

#include "forgewindow.h"

int main(int argc, char *argv[])
{
    QApplication app(argc, argv);
    QApplication::setApplicationName("fileapi forge");
    QApplication::setStyle(QStyleFactory::create("Fusion"));
    ForgeWindow w;
    w.showFullScreen();  // fills the page, without a window frame
    return app.exec();
}
