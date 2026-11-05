# Qt for WebAssembly web UI for the fileapi git forge. Build with `just ui` (see ui/README.md).
cmake_minimum_required(VERSION 3.21)
project(forgeui LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 REQUIRED COMPONENTS Core Gui Widgets Network)

qt_add_executable(forgeui main.cpp forgewindow.cpp forgewindow.h api.cpp api.h highlighter.cpp highlighter.h codeview.cpp codeview.h)
target_link_libraries(forgeui PRIVATE Qt6::Core Qt6::Gui Qt6::Widgets Qt6::Network)

if(EMSCRIPTEN)
    # Smaller output; the app has no need for exceptions or a big initial heap.
    target_link_options(forgeui PRIVATE -Os)
    target_compile_options(forgeui PRIVATE -Os)
endif()
