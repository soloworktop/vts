import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from video_to_summary.desktop.app import main
    main()
